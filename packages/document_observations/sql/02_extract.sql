CREATE TABLE extracted AS
SELECT asset_id, original_name,
       extract_observations(payload,
         (SELECT coalesce(json_group_object(name, value_json)::VARCHAR, '{}') FROM run_params)) AS payload
FROM parsed;
