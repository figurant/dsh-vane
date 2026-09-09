"""Portable example package. Domain extraction belongs here, not in the connector."""
import base64
import csv
import io
import json
import os
import re
from pathlib import Path


def parse_asset(path: str, media_type: str) -> str:
    p = Path(path)
    result = {"title": p.stem, "markdown": "", "evidence": [], "observations": [], "media_type": media_type, "path": path}
    if media_type == "text/csv":
        text = p.read_text(encoding="utf-8-sig")
        result["markdown"] = text
        for row_number, row in enumerate(csv.DictReader(io.StringIO(text)), 2):
            key = str(row_number)
            result["evidence"].append({"key": key, "locator": {"row": row_number}, "quote": json.dumps(row, ensure_ascii=False)})
            value = row.get("value")
            result["observations"].append({"item": row.get("item"), "value": float(value) if value not in (None, "") else None, "category": row.get("category") or None, "key": key})
    elif media_type == "application/pdf":
        import pymupdf
        pages = []
        with pymupdf.open(p) as doc:
            for n, page in enumerate(doc, 1):
                text = page.get_text()
                pages.append(text)
                result["evidence"].append({"key": str(n), "locator": {"page": n}, "quote": text})
                # The example's declared text grammar is "item,value[,category]".
                # Unrecognized prose remains evidence; it is not fabricated as a fact.
                for line in text.splitlines():
                    match = re.fullmatch(r"\s*([\w -]+)\s*[,：:]\s*(-?\d+(?:\.\d+)?)\s*(?:,\s*(.*))?", line)
                    if match:
                        result["observations"].append({"item": match[1].strip(), "value": float(match[2]), "category": match[3] or None, "key": str(n)})
        result["markdown"] = "\n\n".join(pages)
    elif media_type.startswith("image/"):
        from PIL import Image
        with Image.open(p) as image:
            image.verify()
        result["markdown"] = "Image registered; a configured vision model is required to read its content."
    else:
        raise ValueError("This package supports CSV, PDF and images; use SQL for Parquet.")
    return json.dumps(result, ensure_ascii=False)


def extract_observations(parsed: str, params: str) -> str:
    data = json.loads(parsed)
    # run_params.value_json contains canonical JSON text. Aggregate those VARCHARs
    # before decoding: Vane 0.1.0 cannot overload json_group_object for JSON values.
    options = {key: json.loads(value) for key, value in json.loads(params).items()}
    alias = options.get("model_alias")
    if alias:
        import httpx
        import jsonschema
        models = json.loads(os.environ.get("DSH_VANE_MODELS", "[]"))
        model = next((m for m in models if m["alias"] == alias), None)
        if model is None:
            raise ValueError("MODEL_NOT_CONFIGURED: unknown extraction model alias")
        prompt = (Path(__file__).parents[1] / "prompts/extract.md").read_text()
        prompt += "\n" + options.get("prompt", "")
        content = [{"type": "text", "text": prompt + "\nDocument:\n" + data["markdown"]}]
        if data["media_type"].startswith("image/"):
            encoded = base64.b64encode(Path(data["path"]).read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{data['media_type']};base64,{encoded}"}})
        elif data["media_type"] == "application/pdf" and not data["markdown"].strip():
            import pymupdf
            with pymupdf.open(data["path"]) as doc:
                if len(doc) > 8:
                    raise ValueError("MODEL_LIMIT: scanned PDF exceeds the example package's 8-page image limit")
                for page in doc:
                    encoded = base64.b64encode(page.get_pixmap(matrix=pymupdf.Matrix(1, 1)).tobytes("png")).decode()
                    content.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}})
        headers = {}
        if model.get("apiKeyEnv"):
            secret = os.environ.get(model["apiKeyEnv"])
            if not secret:
                raise ValueError("MODEL_CONFIG_ERROR: missing configured credential")
            headers["Authorization"] = "Bearer " + secret
        try:
            response = httpx.post(model["baseURL"].rstrip("/") + "/chat/completions", headers=headers,
                                  json={"model": model["model"], "messages": [{"role": "user", "content": content}], "stream": False,
                                        "temperature": model.get("temperature", 0), "max_tokens": model.get("maxTokens", 2048)}, timeout=60)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            extracted = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
            schema = options.get("response_schema", {"type": "object", "required": ["observations"], "properties": {"observations": {"type": "array", "items": {"type": "object", "required": ["item", "value", "quote"]}}}})
            jsonschema.validate(extracted, schema)
        except Exception as exc:
            raise ValueError("MODEL_ERROR: extraction request or response validation failed (" + type(exc).__name__ + ")") from None
        data["observations"], data["evidence"] = [], []
        for n, row in enumerate(extracted["observations"], 1):
            location = {}
            if data["media_type"] == "application/pdf" and isinstance(row.get("page"), int) and row["page"] >= 1:
                location["page"] = row["page"]
            bbox = row.get("bbox")
            if data["media_type"].startswith("image/") and isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(x, (int, float)) and 0 <= x <= 1 for x in bbox) and bbox[0] <= bbox[2] and bbox[1] <= bbox[3]:
                location["bbox"] = bbox
            key = str(n)
            data["evidence"].append({"key": key, "locator": location, "quote": str(row["quote"])})
            data["observations"].append({"item": row["item"], "value": row["value"], "category": row.get("category"), "key": key})
        data["model_response"] = text
    elif data["media_type"].startswith("image/") or (data["media_type"] == "application/pdf" and not data["markdown"].strip()):
        raise ValueError("MODEL_REQUIRED: configure and select a vision model for images/scanned PDFs")
    for row in data["observations"]:
        if row["value"] is not None:
            row["value"] = float(row["value"]) * options.get("multiplier", 1)
    return json.dumps(data, ensure_ascii=False)
