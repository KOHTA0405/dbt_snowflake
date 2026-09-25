{{ config(
    materialized='table',
    table_format='iceberg',
    iceberg_version=3,
    external_volume='ICEBERG_' ~ (target.name | upper)
) }}

select 1 as id
