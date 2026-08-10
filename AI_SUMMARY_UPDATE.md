# AI Portfolio Summary update

The dashboard placeholder is now connected to an authenticated `POST /ai-summary`
endpoint. It sends an organisation-scoped snapshot of active holdings and current
dashboard metrics to the OpenAI Responses API and renders a structured summary.

## Local configuration

Add these values to the existing local `.env` file (do not commit it):

```text
OPENAI_API_KEY=your_api_key_here
OPENAI_MODEL=gpt-5-mini
```

`OPENAI_MODEL` is optional and defaults to `gpt-5-mini`.

Install the added dependency:

```powershell
python -m pip install -r requirements.txt
```

## Data and safety boundary

- Only active holdings for the logged-in user's organisation are included.
- No username, email address or organisation name is sent to the model.
- Responses are requested with `store=False`.
- The model receives no external news or client suitability data.
- The fixed management-information disclaimer remains visible above every output.
- The endpoint returns a clear error without breaking the dashboard if the API key
  is missing or the service is temporarily unavailable.
