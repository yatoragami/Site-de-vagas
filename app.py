import concurrent.futures
import datetime
import html
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlparse

ROOT = Path(__file__).resolve().parent
MAX_REQUEST_BYTES = 18 * 1024 * 1024
MAX_REMOTIVE_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_SEARCH_RESULTS = 100
MAX_LINKEDIN_DETAIL_FETCHES = 80
RECENT_DAYS = 30
REMOTIVE_CACHE_SECONDS = 6 * 60 * 60
LINKEDIN_SEARCH_CACHE_SECONDS = 15 * 60
LINKEDIN_DETAIL_CACHE_SECONDS = 30 * 60
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SENIOR_LEVEL = re.compile(
    r"\b(?:senior|sr\.?|pleno|mid[- ]level|principal|staff|lead|"
    r"especialista|specialist|gerente|manager|diretor|director|head)\b",
    re.IGNORECASE,
)
NON_IT_TITLE = re.compile(
    r"\b(?:recruiter|recruitment|sales|marketing|human resources|"
    r"\bhr\b|accountant|accounting|customer success|business development|"
    r"sdr|bdr|finance|financial|vendas|recrutamento|recursos humanos)\b",
    re.IGNORECASE,
)
IT_TITLE = re.compile(
    r"\b(?:ti|it|informatica|computer|computing|tecnologia|technology|sistemas|systems|software|"
    r"front[- ]?end|back[- ]?end|full[- ]?stack|mobile|web|ux|ui|"
    r"desenvolvedor|desenvolvimento|developer|programador|programming|"
    r"engenheir[oa]|engineer|qa|quality|teste|testing|dados|data|"
    r"infraestrutura|infrastructure|network|redes|cyber|cybersecurity|security|"
    r"seguranca|suporte|support|service desk|help desk|devops|cloud|"
    r"database|banco de dados|telecom|automacao|automation|hardware|"
    r"artificial intelligence|machine learning|data science|business intelligence|"
    r"bi|erp|sap|sre)\b",
    re.IGNORECASE,
)
LINKEDIN_SEARCH_TERMS = (
    "estágio TI",
    "estágio desenvolvimento",
    "estágio suporte",
    "junior developer",
    "junior analyst IT",
    "software engineer",
    "programador sistemas",
    "analista de sistemas",
    "analista de TI",
    "analista dados BI",
    "analista QA segurança",
    "assistente TI suporte",
    "service desk support",
    "network cloud DevOps engineer",
)
_REMOTIVE_CACHE = {"expires_at": 0, "jobs": None}
_REMOTIVE_CACHE_LOCK = threading.Lock()
_LINKEDIN_SEARCH_CACHE = {}
_LINKEDIN_DETAIL_CACHE = {}
_LINKEDIN_CACHE_LOCK = threading.Lock()


def fold_text(value):
    normalized = unicodedata.normalize("NFKD", value or "")
    return "".join(character for character in normalized if not unicodedata.combining(character)).casefold()


class LinkedInCardParser(HTMLParser):
    FIELD_CLASSES = {
        "base-search-card__title": "title",
        "base-search-card__subtitle": "company",
        "job-search-card__location": "location",
        "job-search-card__listdate": "posted_label",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jobs = []
        self.current = None
        self.card_depth = 0
        self.capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        if self.current is None and tag == "div" and "base-search-card" in classes:
            urn = attrs.get("data-entity-urn", "")
            self.current = {
                "id": urn.rsplit(":", 1)[-1],
                "url": "",
                "title": "",
                "company": "",
                "location": "",
                "posted_date": "",
                "posted_label": "",
            }
            self.card_depth = 1
        elif self.current is not None and tag == "div":
            self.card_depth += 1

        if self.current is None:
            return

        if self.capture is not None:
            self.capture["depth"] += 1

        if tag == "a" and "base-card__full-link" in classes:
            self.current["url"] = attrs.get("href", "").split("?", 1)[0]
        if tag == "time" and "job-search-card__listdate" in classes:
            self.current["posted_date"] = attrs.get("datetime", "")

        field = next((name for class_name, name in self.FIELD_CLASSES.items() if class_name in classes), None)
        if field:
            self.capture = {"field": field, "tag": tag, "depth": 0, "parts": []}

    def handle_data(self, data):
        if self.capture is not None and self.current is not None:
            self.capture["parts"].append(data)

    def handle_endtag(self, tag):
        if self.capture is not None:
            if tag == self.capture["tag"] and self.capture["depth"] == 0:
                value = " ".join(" ".join(self.capture["parts"]).split())
                self.current[self.capture["field"]] = value
                self.capture = None
            elif self.capture["depth"] > 0:
                self.capture["depth"] -= 1

        if self.current is not None and tag == "div":
            self.card_depth -= 1
            if self.card_depth == 0:
                if self.current["title"] and self.current["url"]:
                    self.jobs.append(self.current)
                self.current = None


class JobPostingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_json_ld = False
        self.json_parts = []
        self.posting = None

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            if self.in_json_ld:
                self.json_depth += 1
            return
        attrs = dict(attrs)
        if attrs.get("type", "").lower() == "application/ld+json":
            self.in_json_ld = True
            self.json_parts = []

    def handle_data(self, data):
        if self.in_json_ld:
            self.json_parts.append(data)

    def handle_endtag(self, tag):
        if not self.in_json_ld:
            return
        if tag == "script":
            self.in_json_ld = False
            try:
                payload = json.loads("".join(self.json_parts))
            except json.JSONDecodeError:
                return
            records = payload if isinstance(payload, list) else [payload]
            self.posting = next(
                (record for record in records if isinstance(record, dict) and record.get("@type") == "JobPosting"),
                self.posting,
            )


class RequestError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_POST(self):
        route = urlparse(self.path).path
        if route != "/api/search":
            self.send_json({"error": "Rota não encontrada."}, 404)
            return

        try:
            self.send_json(self.search_jobs(self.read_json_body()))
        except RequestError as error:
            self.send_json({"error": str(error)}, error.status)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise RequestError("Tamanho da solicitação inválido.") from error
        if length < 1 or length > MAX_REQUEST_BYTES:
            raise RequestError("A solicitação excede o limite permitido.")
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RequestError("O corpo da solicitação não contém JSON válido.") from error
        if not isinstance(body, dict):
            raise RequestError("Formato de solicitação inválido.")
        return body

    @staticmethod
    def build_searches(location):
        place = location.strip()[:120] or "Brasil"
        is_brazil_search = fold_text(place) in ("brasil", "brazil")
        return [
            {
                "keywords": term,
                "location": "Brazil" if is_brazil_search else place,
                "geo_id": "106057199" if is_brazil_search else "",
            }
            for term in LINKEDIN_SEARCH_TERMS
        ]

    @staticmethod
    def fetch_linkedin_search(search):
        cache_key = (search["keywords"], search["location"])
        now = time.monotonic()
        with _LINKEDIN_CACHE_LOCK:
            cached = _LINKEDIN_SEARCH_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

        params = {
            "keywords": search["keywords"],
            "location": search["location"],
            "f_TPR": "r2592000",
            "sortBy": "DD",
        }
        if search.get("geo_id"):
            params["geoId"] = search["geo_id"]
        query = urlencode(params)
        request = urllib.request.Request(
            f"https://br.linkedin.com/jobs/search/?{query}",
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            page = response.read(2_500_000).decode("utf-8", errors="replace")
        parser = LinkedInCardParser()
        parser.feed(page)
        for job in parser.jobs:
            job["search_geo_id"] = search.get("geo_id", "")
        with _LINKEDIN_CACHE_LOCK:
            _LINKEDIN_SEARCH_CACHE[cache_key] = (
                now + LINKEDIN_SEARCH_CACHE_SECONDS,
                parser.jobs,
            )
        return parser.jobs

    @staticmethod
    def fetch_remotive_jobs():
        with _REMOTIVE_CACHE_LOCK:
            if _REMOTIVE_CACHE["jobs"] is not None and _REMOTIVE_CACHE["expires_at"] > time.monotonic():
                return _REMOTIVE_CACHE["jobs"]

            request = urllib.request.Request(
                "https://remotive.com/api/remote-jobs",
                headers={"User-Agent": USER_AGENT},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                response_body = response.read(MAX_REMOTIVE_RESPONSE_BYTES + 1)
            if len(response_body) > MAX_REMOTIVE_RESPONSE_BYTES:
                raise ValueError("A fonte Remotive retornou dados acima do limite permitido.")
            payload = json.loads(response_body.decode("utf-8"))
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            if not isinstance(jobs, list):
                raise ValueError("A fonte Remotive retornou dados inválidos.")

            _REMOTIVE_CACHE.update({
                "expires_at": time.monotonic() + REMOTIVE_CACHE_SECONDS,
                "jobs": jobs,
            })
            return jobs

    @staticmethod
    def normalize_remotive_job(posting):
        if not isinstance(posting, dict):
            return None
        url = posting.get("url", "")
        parsed_url = urlparse(url)
        if parsed_url.scheme != "https" or parsed_url.hostname != "remotive.com":
            return None

        description_html = html.unescape(str(posting.get("description") or ""))
        description = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", description_html)).split())
        tags = posting.get("tags")
        searchable_description = description
        if isinstance(tags, list):
            searchable_description += " " + " ".join(str(tag) for tag in tags)
        required_location = str(posting.get("candidate_required_location") or "").strip()
        return {
            "id": str(posting.get("id") or ""),
            "title": str(posting.get("title") or ""),
            "company": str(posting.get("company_name") or "Empresa não informada"),
            "location": required_location or "Remoto",
            "work_mode": "Remoto",
            "posted_date": str(posting.get("publication_date") or "")[:10],
            "url": url,
            "snippet": description[:280] + ("…" if len(description) > 280 else ""),
            "description": searchable_description,
            "candidate_required_location": required_location,
            "source": "Remotive",
        }

    @staticmethod
    def is_remotive_brazil_eligible(job):
        location = fold_text(job.get("candidate_required_location", ""))
        return re.search(
            r"\b(?:worldwide|anywhere|brazil|brasil|latin america|south america)\b",
            location,
        ) is not None

    @staticmethod
    def parse_job_posting(job):
        parsed_url = urlparse(job["url"])
        if parsed_url.hostname not in ("linkedin.com", "www.linkedin.com", "br.linkedin.com"):
            return job
        with _LINKEDIN_CACHE_LOCK:
            cached = _LINKEDIN_DETAIL_CACHE.get(job["url"])
            if cached and cached[0] > time.monotonic():
                return {**job, **cached[1]}

        request = urllib.request.Request(
            job["url"],
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.7",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                page = response.read(1_500_000).decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, ValueError):
            return job
        parser = JobPostingParser()
        parser.feed(page)
        posting = parser.posting
        if not posting:
            with _LINKEDIN_CACHE_LOCK:
                _LINKEDIN_DETAIL_CACHE[job["url"]] = (
                    time.monotonic() + LINKEDIN_DETAIL_CACHE_SECONDS,
                    {},
                )
            return job

        organization = posting.get("hiringOrganization")
        if isinstance(organization, dict) and organization.get("name"):
            job["company"] = organization["name"]
        description = posting.get("description")
        if isinstance(description, str):
            text = re.sub(r"<[^>]+>", " ", html.unescape(description))
            text = " ".join(html.unescape(text).split())
            job["snippet"] = text[:280] + ("…" if len(text) > 280 else "")
            job["description"] = text[:8_000]
        date_posted = posting.get("datePosted")
        if isinstance(date_posted, str):
            job["posted_date"] = date_posted[:10]
        valid_through = posting.get("validThrough")
        if isinstance(valid_through, str):
            job["valid_through"] = valid_through[:10]
        job["job_location_type"] = str(posting.get("jobLocationType") or "")
        job["location_countries"] = []

        def collect_location(value):
            if isinstance(value, dict):
                address = value.get("address")
                if isinstance(address, dict):
                    country = address.get("addressCountry")
                    if isinstance(country, str) and country:
                        job["location_countries"].append(country)
                    elif isinstance(country, dict) and country.get("name"):
                        job["location_countries"].append(country["name"])
                for key in ("name", "addressLocality", "addressRegion"):
                    if isinstance(value.get(key), str):
                        job["location_countries"].append(value[key])
                for nested in value.values():
                    if isinstance(nested, (dict, list)):
                        collect_location(nested)
            elif isinstance(value, list):
                for item in value:
                    collect_location(item)

        collect_location(posting.get("jobLocation"))
        job["applicant_location"] = posting.get("applicantLocationRequirements", "")
        cached_fields = {
            key: value for key, value in job.items()
            if key in (
                "company", "snippet", "description", "posted_date", "valid_through",
                "job_location_type", "location_countries", "applicant_location",
            )
        }
        with _LINKEDIN_CACHE_LOCK:
            _LINKEDIN_DETAIL_CACHE[job["url"]] = (
                time.monotonic() + LINKEDIN_DETAIL_CACHE_SECONDS,
                cached_fields,
            )
        return job

    @staticmethod
    def actual_work_mode(job):
        explicit_type = fold_text(job.get("job_location_type", ""))
        title = fold_text(job.get("title", ""))
        location = fold_text(job.get("location", ""))
        if "hybrid" in explicit_type or "hybrid" in title or "hibrid" in title:
            return "Híbrido"
        if "telecommute" in explicit_type or "remote" in explicit_type:
            return "Remoto"
        if re.search(r"\b(?:remote|remot[oa]|teletrabalho|home office)\b", title + " " + location):
            return "Remoto"
        if job.get("location_countries") or job.get("location"):
            return "Presencial/Não informado"
        return "Não informado"

    @staticmethod
    def is_brazilian_job(job):
        locations = [fold_text(value) for value in job.get("location_countries", [])]
        locations.append(fold_text(job.get("location", "")))
        foreign_countries = {
            "us", "usa", "united states", "united states of america",
            "ca", "canada", "gb", "uk", "united kingdom", "de", "germany",
            "fr", "france", "in", "india", "au", "australia",
        }
        if any(location in foreign_countries for location in locations):
            return False
        if any(
            re.search(r"\b(?:br|brazil|brasil)\b", value)
            for value in locations
        ):
            return True
        return job.get("search_geo_id") == "106057199"

    @staticmethod
    def is_brazil_eligible_remote(job):
        regions = fold_text(
            " ".join(
                (
                    job.get("location", ""),
                    job.get("description", ""),
                    str(job.get("location_countries", "")),
                    str(job.get("applicant_location", "")),
                )
            )
        )
        applicant = fold_text(str(job.get("applicant_location", "")))
        countries = [fold_text(country) for country in job.get("location_countries", [])]
        if any(country in ("us", "united states", "united states of america") for country in countries):
            if not any(term in applicant or term in regions for term in ("worldwide", "anywhere", "latin america", "south america", "brazil", "brasil")):
                return False
        if any(term in regions for term in ("estados unidos", "united states", "usa", "canada", "europe", "india")):
            if not any(term in applicant for term in ("worldwide", "anywhere", "latin america", "south america", "brazil", "brasil")):
                return False
        return bool(
            any(country in ("br", "brazil", "brasil") for country in countries)
            or any(term in applicant for term in ("brazil", "brasil", "worldwide", "anywhere", "latin america", "south america"))
            or re.search(r"\b(?:brazil|brasil|worldwide|anywhere|latin america|south america)\b", regions)
        )

    @staticmethod
    def valid_recent_job(job, cutoff):
        title = job.get("title", "")
        posted = job.get("posted_date", "")
        try:
            posted_day = datetime.date.fromisoformat(posted[:10])
        except (ValueError, TypeError):
            return False
        if posted_day < cutoff or posted_day > datetime.date.today():
            return False

        normalized_title = fold_text(title)
        if SENIOR_LEVEL.search(normalized_title) or NON_IT_TITLE.search(normalized_title):
            return False
        if not IT_TITLE.search(normalized_title):
            return False
        valid_through = job.get("valid_through", "")
        if valid_through:
            try:
                if datetime.date.fromisoformat(valid_through[:10]) < datetime.date.today():
                    return False
            except ValueError:
                return False
        return True

    @classmethod
    def search_jobs(cls, body):
        location = body.get("location", "Brasil")
        preference = body.get("preference", "all")
        if not isinstance(location, str) or not isinstance(preference, str):
            raise RequestError("Localidade ou preferência de trabalho inválida.")
        if preference not in ("all", "remote", "hybrid", "onsite"):
            raise RequestError("Selecione uma modalidade de trabalho válida.")

        searches = cls.build_searches(location)
        results = []
        linkedin_failures = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(cls.fetch_linkedin_search, search): search for search in searches}
            remotive_future = pool.submit(cls.fetch_remotive_jobs) if preference in ("all", "remote") else None
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.extend(future.result())
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    linkedin_failures += 1
            remotive_jobs = []
            remotive_succeeded = False
            if remotive_future is not None:
                try:
                    remotive_jobs = remotive_future.result()
                    remotive_succeeded = True
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    pass

        deduplicated = {}
        for job in results:
            if job["url"] not in deduplicated:
                deduplicated[job["url"]] = job
        cutoff = datetime.date.today() - datetime.timedelta(days=RECENT_DAYS)
        candidates = []
        for job in deduplicated.values():
            if cls.valid_recent_job(job, cutoff):
                candidates.append(job)
        candidates.sort(key=lambda job: job.get("posted_date", ""), reverse=True)

        detailed = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(cls.parse_job_posting, job)
                for job in candidates[:MAX_LINKEDIN_DETAIL_FETCHES]
            ]
            for future in concurrent.futures.as_completed(futures):
                try:
                    detailed.append(future.result())
                except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                    continue
        detailed.sort(key=lambda job: job.get("posted_date", ""), reverse=True)

        remotive_candidates = []
        for posting in remotive_jobs:
            job = cls.normalize_remotive_job(posting)
            if not job or not cls.is_remotive_brazil_eligible(job):
                continue
            if cls.valid_recent_job(job, cutoff):
                remotive_candidates.append(job)

        jobs = []
        all_candidates = sorted(
            [*detailed, *remotive_candidates],
            key=lambda job: job.get("posted_date", ""),
            reverse=True,
        )
        for job in all_candidates:
            actual_mode = "Remoto" if job.get("source") == "Remotive" else cls.actual_work_mode(job)
            if preference == "hybrid" and actual_mode != "Híbrido":
                continue
            if preference == "remote" and actual_mode != "Remoto":
                continue
            if preference == "onsite" and actual_mode not in ("Presencial/Não informado", "Não informado"):
                continue
            if actual_mode == "Remoto" and job.get("source") == "Remotive" and not cls.is_remotive_brazil_eligible(job):
                continue
            if (
                actual_mode == "Remoto"
                and job.get("source") != "Remotive"
                and not cls.is_brazil_eligible_remote(job)
                and not cls.is_brazilian_job(job)
            ):
                continue
            if (
                location.strip().casefold() in ("brasil", "brazil")
                and actual_mode != "Remoto"
                and not cls.is_brazilian_job(job)
            ):
                continue
            if location.strip().casefold() not in ("", "brasil", "brazil"):
                location_query = fold_text(re.sub(r"\s+", " ", re.sub(r"[,/]+", " ", location))).strip()
                job_location = fold_text(re.sub(r"\s+", " ", re.sub(r"[,/]+", " ", job.get("location", "")))).strip()
                if actual_mode != "Remoto" and location_query not in job_location:
                    continue
                if actual_mode == "Remoto" and job.get("source") == "Remotive":
                    if not cls.is_remotive_brazil_eligible(job):
                        continue
            job["work_mode"] = actual_mode
            jobs.append({
                "id": job.get("id", ""),
                "title": job.get("title", ""),
                "company": job.get("company", "Empresa não informada"),
                "location": job.get("location", ""),
                "work_mode": job.get("work_mode", ""),
                "posted_date": job.get("posted_date", ""),
                "posted_label": job.get("posted_label", ""),
                "url": job.get("url", ""),
                "snippet": job.get("snippet", ""),
                "source": job.get("source", "LinkedIn"),
            })
            if len(jobs) >= MAX_SEARCH_RESULTS:
                break

        linkedin_succeeded = len(searches) > linkedin_failures
        if not linkedin_succeeded and not remotive_succeeded:
            raise RequestError(
                "Não foi possível consultar os anúncios públicos agora. Tente novamente em alguns minutos.",
                502,
            )
        successful_searches = len(searches) - linkedin_failures + int(remotive_succeeded)
        requested_searches = len(searches) + int(preference in ("all", "remote"))
        return {
            "jobs": jobs,
            "searched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "window_days": RECENT_DAYS,
            "sources": (["LinkedIn"] if linkedin_succeeded else [])
            + (["Remotive"] if remotive_succeeded else []),
            "searches_completed": successful_searches,
            "searches_requested": requested_searches,
            "searches_failed": linkedin_failures
            + int(preference in ("all", "remote") and not remotive_succeeded),
            "results_limit_reached": len(jobs) == MAX_SEARCH_RESULTS,
        }

    def send_json(self, payload, status=200):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), AppHandler)
    print("Veja vagas de TI em http://127.0.0.1:8765")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
