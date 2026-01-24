{{ config(
    materialized='view',
    schema='GOLD'
) }}

select *
from {{ source('dbt_snowflake_monitoring', 'warehouses_type2_dimension') }}
