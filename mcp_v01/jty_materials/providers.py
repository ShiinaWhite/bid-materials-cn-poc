"""Replaceable LLM/Vision provider abstraction.

Business code (Material workflow, consensus engine, project intake) depends on
the VisionProvider / LLMProvider interfaces only, never on a vendor. The v0.7
provider is OpenCode Go (OpenAI chat-completions compatible gateway), but any
compatible endpoint can be configured without touching callers.

Secrets red lines: the API key comes from the environment variable
OPENCODE_GO_API_KEY (preferred) or the deployment-local, git-ignored
config file. It is never written to the database, audit_log, events log,
reports, fixtures or screenshots. Log lines carry provider/model/purpose/
status/latency/response-hash only; Authorization headers are never logged.
"""
import base64
import hashlib
import io
import json
import time
import urllib.error
import urllib.request
import uuid


PROVIDER_ID = 'opencode-go'
DEFAULT_BASE_URL = 'https://opencode.ai/zen/go/v1'
DEFAULT_VISION_MODEL = 'deepseek-v4.1-flash'
DEFAULT_LLM_MODEL = 'deepseek-v4.1-flash'
USER_AGENT = 'bid-materials-poc/0.7.0'
ENV_KEY = 'OPENCODE_GO_API_KEY'
ENV_BASE_URL = 'OPENCODE_GO_BASE_URL'
ENV_MODEL = 'OPENCODE_GO_MODEL'


class ProviderError(Exception):
    """kind: auth (no retry), unavailable (transient, retryable),
    bad_response (schema/JSON failure, one corrective retry allowed)."""
    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def provider_config(config):
    """Resolve provider settings; key resolution order: env > deployment config.
    Returns None when no credential is present at all (caller treats vision/LLM
    as unavailable instead of failing the request)."""
    raw = config.raw.get('vision_provider') or {}
    api_key = __import__('os').environ.get(ENV_KEY) or raw.get('api_key')
    if not api_key:
        return None
    return {
        'provider': PROVIDER_ID,
        'base_url': __import__('os').environ.get(ENV_BASE_URL) or raw.get('base_url') or DEFAULT_BASE_URL,
        'api_key': api_key,
        'vision_model': __import__('os').environ.get(ENV_MODEL) or raw.get('vision_model') or DEFAULT_VISION_MODEL,
        'llm_model': raw.get('llm_model') or DEFAULT_LLM_MODEL,
        'timeout_seconds': int(raw.get('timeout_seconds') or 180),
        'max_retries': int(raw.get('max_retries') or 2),
    }


class OpenCodeGoProvider:
    """One instance == one extraction conversation: the gateway routes and
    caches per x-opencode-session, so a stable session id per run is required."""

    def __init__(self, config):
        settings = provider_config(config)
        if settings is None:
            raise ProviderError('auth', 'no provider credential configured (' + ENV_KEY + ' unset)')
        self.provider = settings['provider']
        self.base_url = settings['base_url'].rstrip('/')
        self.api_key = settings['api_key']
        self.vision_model = settings['vision_model']
        self.llm_model = settings['llm_model']
        self.timeout = settings['timeout_seconds']
        self.max_retries = settings['max_retries']
        self.session = uuid.uuid4()
        self.c = config

    # ------------------------------------------------------------ transport --
    def _log(self, purpose, model, status, latency_ms, content=None, error=None):
        self.c.log('provider.request', provider=self.provider, model=model, purpose=purpose,
                   status=status, latency_ms=latency_ms,
                   response_hash=hashlib.sha256(content.encode('utf-8')).hexdigest()[:16] if content else None,
                   error=type(error).__name__ + ': ' + str(error)[:200] if error else None)

    def _post(self, payload, purpose):
        model = payload['model']
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        attempts = self.max_retries + 1
        last_error = None
        for attempt in range(attempts):
            request = urllib.request.Request(
                self.base_url + '/chat/completions', body,
                {'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json',
                 'Accept': 'application/json', 'User-Agent': USER_AGENT,
                 'x-opencode-session': str(self.session)})
            started = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    data = json.loads(response.read())
                latency = int((time.monotonic() - started) * 1000)
                message = data['choices'][0]['message']
                content = message.get('content') or ''
                self._log(purpose, model, 'ok', latency, content)
                return content
            except urllib.error.HTTPError as error:
                latency = int((time.monotonic() - started) * 1000)
                detail = error.read().decode('utf-8', 'replace')[:300]
                if error.code in (401, 403):
                    self._log(purpose, model, 'auth_failed', latency, error=detail)
                    raise ProviderError('auth', 'provider rejected the credential (HTTP %d)' % error.code)
                retryable = error.code in (408, 409, 429) or error.code >= 500
                self._log(purpose, model, 'http_%d' % error.code, latency, error=detail)
                last_error = ProviderError('unavailable' if retryable else 'bad_response',
                                           'provider HTTP %d: %s' % (error.code, detail))
                if not retryable:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                latency = int((time.monotonic() - started) * 1000)
                self._log(purpose, model, 'transport_error', latency, error=error)
                last_error = ProviderError('unavailable', type(error).__name__ + ': ' + str(error)[:200])
            if attempt < attempts - 1:
                time.sleep(2 * (attempt + 1))
        raise last_error

    # -------------------------------------------------------------- interface --
    def complete(self, prompt, *, purpose, images=None, max_tokens=4000, json_mode=True):
        """One chat completion. images: list of (mime, bytes) rendered pages;
        they are attached as data URLs in-memory and never persisted."""
        content = [{'type': 'text', 'text': prompt}]
        for mime, data in images or ():
            content.append({'type': 'image_url', 'image_url': {
                'url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')}})
        payload = {'model': self.vision_model if images else self.llm_model,
                   'messages': [{'role': 'user', 'content': content}],
                   'max_tokens': max_tokens}
        if json_mode:
            payload['response_format'] = {'type': 'json_object'}
        text = self._post(payload, purpose)
        if json_mode:
            parsed = extract_json(text)
            if parsed is None:
                retry_prompt = prompt + ('\n\n你上一次的输出不是合法 JSON。请重新只输出一个 JSON 对象，'
                                         '不要包含任何其他文字或代码块标记。')
                payload['messages'] = [{'role': 'user', 'content': [
                    {'type': 'text', 'text': retry_prompt}
                    + content[1:]]}]
                text = self._post(payload, purpose + '/json-retry')
                parsed = extract_json(text)
                if parsed is None:
                    raise ProviderError('bad_response', 'provider did not return valid JSON')
            return parsed
        return text

    def classify(self, image_mimes_and_bytes, type_labels, *, purpose):
        """Vision classification over the original page(s). Returns
        (document_type, confidence 0..1) constrained to type_labels."""
        labels = '\n'.join('- ' + code for code in type_labels)
        answer = self.complete(
            '这是投标资料库中一份资料的原件扫描页。请判断它最符合下面哪一个资料类别，只输出 JSON：\n'
            '{"document_type":"<类别代码>","confidence":<0到1的小数>}\n'
            '类别代码（必须完全使用以下之一，不得发明新代码）：\n' + labels
            + '\n如果页面无法辨认或不属于任何类别，document_type 用 "unknown"，confidence 用 0。',
            purpose=purpose, images=image_mimes_and_bytes)
        code = answer.get('document_type')
        confidence = answer.get('confidence')
        if code not in type_labels:
            raise ProviderError('bad_response', 'classification returned unknown type: ' + repr(code)[:80])
        try:
            confidence = min(1.0, max(0.0, float(confidence)))
        except (TypeError, ValueError):
            confidence = 0.0
        return code, confidence

    def extract(self, image_mimes_and_bytes, type_code, field_list, *, purpose):
        """Structured extraction constrained to field_list. Returns a dict of
        field -> raw string value (caller canonicalizes and validates)."""
        fields = '\n'.join('- ' + f for f in field_list)
        return self.complete(
            '这是同一份资料（类别代码 ' + type_code + '）的原件扫描页。请逐字提取下列字段的值，只输出 JSON：\n'
            '{"fields":{"<字段代码>":{"value":"<原文照抄的值>","page":<所在页码，从1开始>}}}\n'
            '字段代码清单（只能使用这些，没有看到的字段不要输出）：\n' + fields
            + '\n要求：\n'
              '1. 值必须逐字符照抄页面原文（保留中文、字母、数字、标点），不要翻译、不要改写、不要纠正、不要推测。\n'
              '2. 编号/代码类字段（如统一社会信用代码、证书编号、专利号）：一个字符一个字符地照抄，'
              '共18位的代码必须是18位，不要增删字符，不要加空格。\n'
              '3. 日期输出 YYYY-MM-DD；金额只输出数字。\n'
              '4. 页面上不存在的字段直接省略；看不清的字段宁可省略，不要猜。',
            purpose=purpose, images=image_mimes_and_bytes)


class NullProvider:
    """Explicit unavailable provider: caller-visible, never a silent fake PASS."""

    def __init__(self, reason='provider not configured'):
        self.reason = reason
        self.provider = 'none'
        self.vision_model = 'none'
        self.llm_model = 'none'

    def _raise(self, *args, **kwargs):
        raise ProviderError('unavailable', self.reason)

    complete = classify = extract = _raise


def vision_provider(config):
    try:
        return OpenCodeGoProvider(config)
    except ProviderError as error:
        config.log('provider.unavailable', reason=str(error))
        return NullProvider(str(error))


def extract_json(text):
    """Tolerant JSON extraction: the gateway may wrap objects in prose or
    fences even in json mode. Returns the first parseable object or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    stripped = text.strip()
    if stripped.startswith('```'):
        stripped = stripped.strip('`')
        if stripped.startswith('json'):
            stripped = stripped[4:]
    try:
        value = json.loads(stripped)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    depth = 0
    start = None
    for index, char in enumerate(stripped):
        if char == '{':
            if depth == 0:
                start = index
            depth += 1
        elif char == '}' and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    value = json.loads(stripped[start:index + 1])
                    if isinstance(value, dict):
                        return value
                except ValueError:
                    pass
                start = None
    return None


def image_data_url(mime, data):
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')


def render_pages_for_vision(path, max_pages=3, max_side=2200):
    """Render the pages actually needed for recognition, in memory only.

    Images are passed through unchanged when small enough (original bytes are
    never modified on disk); PDF pages are rasterized with PyMuPDF at a scale
    that keeps the payload bounded. Nothing is written to disk or the DB.
    Returns [(mime, bytes), ...] where the mime ALWAYS matches the encoded
    bytes (a resized PNG is re-encoded as JPEG and declared as such; without
    Pillow the original bytes pass through with their original mime)."""
    from pathlib import Path
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in ('.png', '.jpg', '.jpeg'):
        mime = 'image/png' if suffix == '.png' else 'image/jpeg'
        return [_bounded_image(mime, path.read_bytes(), max_side)]
    if suffix == '.pdf':
        import pymupdf
        pages = []
        with pymupdf.open(str(path)) as doc:
            for number in range(min(max_pages, doc.page_count)):
                page = doc.load_page(number)
                scale = min(max_side / max(page.rect.width, 1), 2.0)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                pages.append(('image/jpeg', pix.tobytes('jpeg', jpg_quality=88)))
        return pages
    raise ValueError('vision recognition supports PDF/PNG/JPEG originals only')


def _bounded_image(mime, data, max_side):
    """(mime, bytes) transport copy. Resize requires Pillow; any failure —
    including Pillow being absent — degrades safely to the original bytes
    with their original mime, never to a mismatched pair."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return mime, data
    try:
        with Image.open(io.BytesIO(data)) as image:
            if max(image.size) <= max_side:
                return mime, data
            image = ImageOps.exif_transpose(image)
            image.thumbnail((max_side, max_side))
            out = io.BytesIO()
            image.convert('RGB').save(out, 'JPEG', quality=88)
            return 'image/jpeg', out.getvalue()
    except Exception:
        return mime, data
