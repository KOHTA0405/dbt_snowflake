with

products as (

    select * from {{ ref('stg_products') }}

),

product_supply_costs as (

    select * from {{ ref('int_product_supply_costs') }}

),

final as (

    select
        {{ dbt_utils.generate_surrogate_key(['p.product_id']) }} as product_sk,
        p.product_id,
        p.product_name,
        p.product_type,
        p.product_description,
        p.product_price,
        p.is_food_item,
        p.is_drink_item,
        coalesce(psc.total_supply_cost, 0) as supply_cost,
        p.product_price - coalesce(psc.total_supply_cost, 0) as profit_per_unit,
        case
            when p.product_price > 0
            then ((p.product_price - coalesce(psc.total_supply_cost, 0)) / p.product_price) * 100
            else 0
        end as profit_margin_percent

    from products p
    left join product_supply_costs psc
        on p.product_id = psc.product_id

)

select * from final

