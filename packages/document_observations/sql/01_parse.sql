CREATE TABLE parsed AS
SELECT asset_id, original_name, parse_asset(path, media_type) AS payload
FROM input_files;
