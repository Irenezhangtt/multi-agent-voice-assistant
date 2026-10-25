"""Independent judge adapters; business agents retain their own Claude model."""
import os

import httpx
from langsmith import trace


class HTTPJudge:
    def __init__(self, provider, model):
        self.provider, self.model = provider, model
        self.key = os.getenv('JUDGE_API_KEY') or os.getenv('OPENAI_API_KEY')
        if not self.key:
            raise ValueError('Configure JUDGE_API_KEY or OPENAI_API_KEY locally')
        self.base = ('https://api.openai.com/v1' if provider == 'openai'
                     else os.environ['JUDGE_BASE_URL'].rstrip('/'))

    def with_structured_output(self, schema, *, method):
        if method != 'json_schema':
            raise ValueError('Judge requires native JSON schema')
        return StructuredHTTPJudge(self, schema)


class StructuredHTTPJudge:
    def __init__(self, model, schema):
        self.model, self.schema = model, schema

    def invoke(self, messages, config=None):
        model = self.model
        inputs = [{'role': 'system' if m.type == 'system' else 'user', 'content': m.content} for m in messages]
        schema = self.schema.model_json_schema()
        if model.provider == 'openai':
            endpoint = '/responses'
            body = {'model': model.model, 'input': inputs, 'store': False, 'max_output_tokens': 8000,
                    'text': {'format': {'type': 'json_schema', 'name': 'clinic_judgment', 'strict': True, 'schema': schema}}}
        else:
            endpoint = '/chat/completions'
            body = {'model': model.model, 'messages': inputs, 'temperature': 0, 'max_tokens': 3500,
                    'response_format': {'type': 'json_schema', 'json_schema': {'name': 'clinic_judgment', 'strict': True, 'schema': schema}}}
        with trace('clinic_judge_model', run_type='llm', inputs={'messages': inputs},
                   metadata={'ls_provider': model.provider, 'ls_model_name': model.model}) as run:
            response = httpx.post(model.base + endpoint, json=body,
                                  headers={'Authorization': 'Bearer ' + model.key}, timeout=120)
            # Avoid putting provider exception bodies / credentials into evaluation artifacts.
            if response.is_error:
                raise RuntimeError(f'Judge provider HTTP {response.status_code}')
            data = response.json()
            if model.provider == 'openai':
                if data.get('status') != 'completed':
                    raise RuntimeError('Judge response incomplete')
                contents = [c for item in data.get('output', []) if item.get('type') == 'message' for c in item.get('content', [])]
                if any(c.get('type') == 'refusal' for c in contents):
                    raise RuntimeError('Judge refused evaluation')
                answer = ''.join(c['text'] for c in contents if c.get('type') == 'output_text')
            else:
                choice = data['choices'][0]
                if choice.get('finish_reason') != 'stop' or choice['message'].get('refusal'):
                    raise RuntimeError('Judge response incomplete or refused')
                answer = choice['message']['content']
            parsed = self.schema.model_validate_json(answer)
            run.end(outputs={'judgment': parsed.model_dump(), 'usage': data.get('usage')})
            return parsed
