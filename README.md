# Vagas de TI dos últimos 30 dias

## Executar no Windows

Requer Python 3.10 ou superior. No PowerShell, nesta pasta, execute:

```powershell
python app.py
```

Abra `http://127.0.0.1:8765`. A busca começa automaticamente; informe uma localidade e/ou modalidade para filtrar. O servidor escuta apenas em `127.0.0.1`.

## Como a busca funciona

- Consulta anúncios públicos do LinkedIn e o feed público do Remotive. Não varre a maioria dos sites de emprego nem a web inteira.
- Inclui diferentes funções relacionadas a TI sem exigir que o título diga estágio/júnior/assistente. Exclui títulos sênior/pleno e equivalentes, vagas claramente não técnicas, além de anúncios fora do período.
- Filtra anúncios publicados nos últimos 30 dias e ordena pela data de publicação, mais recente primeiro.
- Mostra até 100 resultados por busca para manter a página responsiva.
- O feed do Remotive é armazenado em cache por seis horas para respeitar a frequência de consulta recomendada pelo serviço.
- As buscas e detalhes do LinkedIn são armazenados em cache por 15 e 30 minutos, respectivamente, para reduzir consultas repetidas.
- Os resultados e links apontam para as fontes originais. A aplicação não cobre toda a web; disponibilidade, modalidade e situação da vaga devem ser confirmadas no anúncio original.
