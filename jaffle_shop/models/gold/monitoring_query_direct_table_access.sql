{{ config(
    materialized='view',
    schema='GOLD'
) }}

select *
from {{ source('dbt_snowflake_monitoring', 'query_direct_table_access') }}
