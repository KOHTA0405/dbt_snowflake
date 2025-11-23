{{
    config(
        materialized='view'
    )
}}

with

supplies_with_rank as (

    select
        supply_uuid,
        supply_id,
        product_id,
        supply_name,
        supply_cost,
        is_perishable_supply,
        row_number() over (
            partition by supply_id, product_id
            order by supply_uuid desc
        ) as rn

    from {{ ref('stg_supplies') }}

),

latest_supplies as (

    select
        supply_id,
        product_id,
        supply_name,
        supply_cost,
        is_perishable_supply

    from supplies_with_rank
    where rn = 1

),

product_supply_costs as (

    select
        product_id,
        sum(supply_cost) as total_supply_cost

    from latest_supplies
    group by product_id

)

select * from product_supply_costs

