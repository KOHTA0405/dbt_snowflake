with

order_items as (

    select * from {{ ref('stg_order_items') }}

),

orders as (

    select * from {{ ref('stg_orders') }}

),

products as (

    select * from {{ ref('stg_products') }}

),

product_supply_costs as (

    select * from {{ ref('int_product_supply_costs') }}

),

dim_customers as (

    select * from {{ ref('dim_customers') }}

),

dim_locations as (

    select * from {{ ref('dim_locations') }}

),

dim_products as (

    select * from {{ ref('dim_products') }}

),

order_items_enriched as (

    select
        -- Surrogate keys
        dc.customer_sk,
        dl.location_sk,
        dp.product_sk,

        oi.order_item_id,
        oi.order_id,
        oi.product_id,

        -- Order information
        o.customer_id,
        o.location_id,
        o.ordered_at as order_date,

        -- Product information
        p.product_name,
        p.product_type,
        p.product_price as unit_price,

        -- Supply cost
        coalesce(psc.total_supply_cost, 0) as unit_supply_cost,

        -- Metrics
        p.product_price as revenue_per_item,
        coalesce(psc.total_supply_cost, 0) as cost_per_item,
        p.product_price - coalesce(psc.total_supply_cost, 0) as profit_per_item,
        case
            when p.product_price > 0
            then ((p.product_price - coalesce(psc.total_supply_cost, 0)) / p.product_price) * 100
            else 0
        end as profit_margin_percent

    from order_items oi
    inner join orders o
        on oi.order_id = o.order_id
    inner join products p
        on oi.product_id = p.product_id
    left join product_supply_costs psc
        on oi.product_id = psc.product_id
    left join dim_customers dc
        on o.customer_id = dc.customer_id
    left join dim_locations dl
        on o.location_id = dl.location_id
    left join dim_products dp
        on oi.product_id = dp.product_id

)

select * from order_items_enriched

