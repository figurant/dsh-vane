CREATE TABLE documents AS
SELECT asset_id::VARCHAR AS document_key, original_name::VARCHAR AS title,
       json_extract_string(payload, '$.markdown')::VARCHAR AS markdown FROM extracted;
CREATE TABLE evidence AS
SELECT (e.asset_id || ':' || json_extract_string(j.value, '$.key'))::VARCHAR AS evidence_id,
       e.asset_id::VARCHAR AS asset_id,
       json_extract(j.value, '$.locator')::VARCHAR AS locator_json,
       json_extract_string(j.value, '$.quote')::VARCHAR AS quote
FROM extracted e, json_each(e.payload, '$.evidence') j
ORDER BY e.asset_id, cast(j.key AS BIGINT);
CREATE TABLE facts AS
SELECT json_extract_string(j.value, '$.item')::VARCHAR AS item,
       json_extract(j.value, '$.value')::DOUBLE AS value,
       json_extract_string(j.value, '$.category')::VARCHAR AS category,
       (e.asset_id || ':' || json_extract_string(j.value, '$.key'))::VARCHAR AS evidence_id,
       e.asset_id::VARCHAR AS asset_id
FROM extracted e, json_each(e.payload, '$.observations') j
ORDER BY e.asset_id, cast(j.key AS BIGINT);
