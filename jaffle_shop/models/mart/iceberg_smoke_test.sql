{{ config(
    materialized='table',
    table_format='iceberg',
    external_volume='ICEBERG_' ~ (target.name | upper)
) }}

select 1 as id
