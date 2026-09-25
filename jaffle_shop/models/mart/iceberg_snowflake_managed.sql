{{ config(
    materialized='table',
    table_format='iceberg'
) }}

select 1 as id