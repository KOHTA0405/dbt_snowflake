{{ config(
    materialized='view',
    schema='GOLD'
) }}

select *
from {{ source('dbt_snowflake_monitoring', 'daily_spend') }}
