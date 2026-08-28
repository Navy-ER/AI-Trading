"""
Robust HTTP client bridge.py handles 502, non-JSON responses, markdown fences, SSE streaming.
Drop-in replacement requests.post get_ai_signal.
"""
import requests
import json
import re
import time
import logging
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# SSE Streaming Response Handler
# ──────────────────────────────────────────────────────────────────────

def parse_sse_response(raw_text: str) -> str:
    """
    Parse Server-Sent Events (SSE) streaming response.
    Extracts content from 'data: ' lines, handles [DONE] marker.
    Returns combined text content.
    """
    if not raw_text:
        return ""
    
    lines = raw_text.strip().split('\n')
    content_parts = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith('data: '):
            data = line[6:]  # Remove 'data: ' prefix
            if data == '[DONE]':
                break
            try:
                parsed = json.loads(data)
                # OpenAI-compatible streaming format
                delta = parsed.get('choices', [{}])[0].get('delta', {})
                if 'content' in delta and delta['content']:
                    content_parts.append(delta['content'])
                # Some APIs use 'text' instead of 'delta.content'
                elif 'text' in parsed.get('choices', [{}])[0]:
                    text_val = parsed['choices'][0]['text']
                    if text_val:
                        content_parts.append(text_val)
            except json.JSONDecodeError:
                # Non-JSON data line, skip
                continue
    
    return ''.join(content_parts)


# ──────────────────────────────────────────────────────────────────────
# JSON Extraction (handles markdown fences, chain-of-thought, garbage)
# ──────────────────────────────────────────────────────────────────────

def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Extract valid JSON from messy responses.
    Priority:
    1. Last valid {...} block (model echoes example first, real answer last)
    2. Markdown fenced ```json ... ```
    3. Key-value regex fallback (action=BUY asset=XRP confidence=80%)
    Returns dict or None.
    """
    if not text or not text.strip():
        return None
    
    text = text.strip()
    
    # 1. Try markdown fenced blocks first (```json ... ```)
    fence_pattern = r'```(?:json)?\s*(\{.*?\})\s*```'
    fenced_matches = re.findall(fence_pattern, text, re.DOTALL | re.IGNORECASE)
    for block in reversed(fenced_matches):  # Last valid block wins
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and parsed.get("action") and parsed.get("asset"):
                logger.debug("[HTTP] Extracted JSON from markdown fence")
                return parsed
        except json.JSONDecodeError:
            continue
    
    # 2. Try all {...} blocks, return LAST valid one
    # Pattern matches non-nested {...} (works for flat AI responses)
    block_pattern = r'\{[^{}]*\}'
    blocks = re.findall(block_pattern, text)
    for block in reversed(blocks):  # Last valid block wins
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and parsed.get("action") and parsed.get("asset"):
                logger.debug("[HTTP] Extracted JSON from {...} block")
                return parsed
        except json.JSONDecodeError:
            continue
    
    # 3. Regex fallback for non-JSON text (action=BUY asset=XRPUSDT confidence=80%)
    result = {}
    action_match = re.search(r'"?action"?\s*[:=]\s*"?([A-Z]+)"?', text, re.IGNORECASE)
    if action_match:
        result["action"] = action_match.group(1).upper()
    
    asset_match = re.search(r'"?asset"?\s*[:=]\s*"?([A-Z0-9\-_]+)"?', text, re.IGNORECASE)
    if asset_match:
        result["asset"] = asset_match.group(1).upper()
    
    conf_match = re.search(r'"?confidence"?\s*[:=]\s*"?([0-9.]+)%?"?', text, re.IGNORECASE)
    if conf_match:
        result["confidence"] = conf_match.group(1) + "%"
    
    entry_match = re.search(r'"?entry_?z?o?n?e?"?\s*[:=]\s*"?([0-9.]+)"?', text, re.IGNORECASE)
    if entry_match:
        result["entry_zone"] = float(entry_match.group(1))
    
    tp_match = re.search(r'"?take_?profit"?\s*[:=]\s*"?([0-9.]+)"?', text, re.IGNORECASE)
    if tp_match:
        result["take_profit"] = float(tp_match.group(1))
    
    sl_match = re.search(r'"?stop_?loss"?\s*[:=]\s*"?([0-9.]+)"?', text, re.IGNORECASE)
    if sl_match:
        result["stop_loss"] = float(sl_match.group(1))
    
    reason_match = re.search(r'"?reason"?\s*[:=]\s*"?([^"]+)"?', text, re.IGNORECASE)
    if reason_match:
        result["reason"] = reason_match.group(1).strip()
    
    if result.get("action") and result.get("asset"):
        logger.debug("[HTTP] Extracted JSON via regex fallback")
        return result
    
    return None


# ──────────────────────────────────────────────────────────────────────
# Robust POST with retry, 502 handling, JSON extraction, SSE parsing
# ──────────────────────────────────────────────────────────────────────

def robust_post(
    url: str,
    json_payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: Tuple[int, int] = (60, 180),
    max_retries: int = 3,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Robust POST request with:
    - Explicit "stream": False in payload
    - Exponential backoff retry on 5xx, timeout, connection errors
    - SSE streaming response parsing
    - JSON extraction from messy responses (markdown, chain-of-thought)
    - Returns (parsed_json_dict, raw_content) or (None, error_message)
    
    Usage:
        data, error = robust_post(url, payload, headers)
        if data:
            # success, data is dict
        else:
            # failed, error has reason
    """
    last_error = None
    
    # Ensure stream: False is in payload
    if "stream" not in json_payload:
        json_payload["stream"] = False
    
    for attempt in range(max_retries):
        try:
            logger.debug(f"[HTTP] POST attempt {attempt + 1}/{max_retries} to {url}")
            response = requests.post(
                url,
                json=json_payload,
                headers=headers,
                timeout=timeout,
            )
            
            # ──────────────────────────────────────────────────────────
            # Handle HTTP errors (502, 503, 504, etc.)
            # ──────────────────────────────────────────────────────────
            if response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(f"[HTTP] Server error {response.status_code} on attempt {attempt + 1}")
                
                # Don't retry on 4xx (client errors) - only 5xx
                if response.status_code < 500:
                    return None, f"Client error {response.status_code}: {response.text[:200]}"
                
                # Retry with backoff
                if attempt < max_retries - 1:
                    delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                    logger.info(f"[HTTP] Retrying in {delay:.1f}s...")
                    time.sleep(delay)
                    continue
                return None, last_error
            
            # ──────────────────────────────────────────────────────────
            # Success (2xx) - extract JSON from response
            # ──────────────────────────────────────────────────────────
            response.raise_for_status()
            raw_content = response.text.strip()
            
            if not raw_content:
                last_error = "Empty response body"
                logger.warning(f"[HTTP] Empty response on attempt {attempt + 1}")
                if attempt < max_retries - 1:
                    time.sleep(base_delay)
                    continue
                return None, last_error
            
            # Check if response is SSE streaming format
            is_sse = raw_content.startswith('data: ') or '\ndata: ' in raw_content
            
            if is_sse:
                # Parse SSE streaming response
                combined_content = parse_sse_response(raw_content)
                if combined_content:
                    extracted = extract_json_from_text(combined_content)
                    if extracted:
                        return extracted, combined_content
                    last_error = f"JSON extraction failed from SSE content: {combined_content[:200]}"
                else:
                    last_error = "SSE parsing yielded empty content"
            
            # Try standard JSON first (non-streaming or fallback)
            try:
                data = response.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                if content:
                    extracted = extract_json_from_text(content)
                    if extracted:
                        return extracted, content
                    last_error = f"JSON extraction failed from: {content[:200]}"
                else:
                    last_error = "Empty content in response"
            except json.JSONDecodeError:
                # Response itself is not JSON (e.g., HTML error page)
                if not is_sse:  # Already tried SSE parsing above
                    extracted = extract_json_from_text(raw_content)
                    if extracted:
                        return extracted, raw_content
                    last_error = f"Response not JSON and extraction failed: {raw_content[:200]}"
            
            # If extraction failed, maybe retry (model might be flaky)
            if attempt < max_retries - 1:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                logger.warning(f"[HTTP] Extraction failed, retrying in {delay:.1f}s: {last_error}")
                time.sleep(delay)
                continue
            
            return None, last_error
            
        except requests.exceptions.Timeout:
            last_error = f"Timeout after {timeout[1]}s"
            logger.warning(f"[HTTP] Timeout on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                logger.info(f"[HTTP] Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue
            return None, last_error
            
        except requests.exceptions.ConnectionError as e:
            last_error = f"Connection error: {e}"
            logger.warning(f"[HTTP] Connection error on attempt {attempt + 1}: {e}")
            if attempt < max_retries - 1:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                logger.info(f"[HTTP] Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue
            return None, last_error
            
        except requests.exceptions.RequestException as e:
            last_error = f"Request error: {e}"
            logger.error(f"[HTTP] Request exception: {e}")
            if attempt < max_retries - 1:
                delay = min(base_delay * (backoff_factor ** attempt), max_delay)
                time.sleep(delay)
                continue
            return None, last_error
            
        except Exception as e:
            last_error = f"Unexpected error: {e}"
            logger.exception(f"[HTTP] Unexpected error on attempt {attempt + 1}")
            if attempt < max_retries - 1:
                time.sleep(base_delay)
                continue
            return None, last_error
    
    return None, last_error or "Max retries exceeded"


# ──────────────────────────────────────────────────────────────────────
# Convenience wrapper matching bridge.py get_ai_signal pattern
# ──────────────────────────────────────────────────────────────────────

def call_ai_signal(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    max_retries: int = 3,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Drop-in for the requests.post call in get_ai_signal.
    Ensures "stream": False is set.
    Returns (signal_dict, raw_content) or (None, error).
    """
    # Ensure stream: False
    payload = dict(payload)
    payload["stream"] = False
    
    return robust_post(
        url=url,
        json_payload=payload,
        headers=headers,
        timeout=(60, 180),
        max_retries=max_retries,
        base_delay=2.0,
        max_delay=30.0,
        backoff_factor=2.0,
    )


# ──────────────────────────────────────────────────────────────────────
# Self-test
# ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    
    # Test parse_sse_response
    print("Testing parse_sse_response...")
    sse_test = """data: {"choices":[{"delta":{"content":"Hello"}}]}
data: {"choices":[{"delta":{"content":" world"}}]}
data: [DONE]"""
    result = parse_sse_response(sse_test)
    print(f"  SSE parse: {result!r}")
    assert result == "Hello world", f"Expected 'Hello world', got {result!r}"
    
    # Test extract_json_from_text
    test_cases = [
        # Markdown fence
        ('```json\n{"action":"BUY","asset":"XRPUSDT","confidence":"75%"}\n```',
         {"action": "BUY", "asset": "XRPUSDT", "confidence": "75%"}),
        # Prose JSON
        ('Berikut sinyal: {"action":"SELL","asset":"DOGEUSD","entry_zone":0.15} semoga membantu',
         {"action": "SELL", "asset": "DOGEUSD", "entry_zone": 0.15}),
        # Echo then real answer (last wins)
        ('Contoh: {"action":"BUY","asset":"XRPUSDT","confidence":"75%"} Jawaban: {"action":"SELL","asset":"DOGEUSD","confidence":"80%"}',
         {"action": "SELL", "asset": "DOGEUSD", "confidence": "80%"}),
        # Key-value fallback
        ('action=BUY asset=MUBUSDT confidence=80% entry=0.5',
         {"action": "BUY", "asset": "MUBUSDT", "confidence": "80%", "entry_zone": 0.5}),
        # Non-JSON text
        ("maaf saya tidak bisa membantu", None),
        # Truncated JSON parser extracts partial (useful behavior)
        ('{"action":"BUY","asset":"BTCUSDT"',
         {"action": "BUY", "asset": "BTCUSDT"}),
    ]
    
    print("\nTesting extract_json_from_text...")
    for i, (input_text, expected) in enumerate(test_cases):
        result = extract_json_from_text(input_text)
        status = "PASS" if result == expected else "FAIL"
        print(f"  Test {i+1}: {status} - got {result!r}, expected {expected!r}")
    
    print("\nAll tests complete.")