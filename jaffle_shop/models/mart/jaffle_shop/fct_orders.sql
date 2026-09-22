with

order_items as (

    select * from {{ ref('fct_order_items') }}

),

orders as (

    select * from {{ ref('stg_orders') }}

),

order_metrics as (

    select
        -- Surrogate keys (using first occurrence per order)
        max(oi.customer_sk) as customer_sk,
        max(oi.location_sk) as location_sk,

        oi.order_id,
        oi.customer_id,
        oi.location_id,
        oi.order_date,

        -- Order totals from orders table
        o.order_total as order_total_revenue,
        o.subtotal as order_subtotal,
        o.tax_paid as order_tax,

        -- Aggregated metrics from order items
        count(distinct oi.order_item_id) as item_count,
        sum(oi.revenue_per_item) as calculated_revenue,
        sum(oi.cost_per_item) as total_supply_cost,
        sum(oi.profit_per_item) as total_profit,

        -- Profit margin
        case
            when sum(oi.revenue_per_item) > 0
            then (sum(oi.profit_per_item) / sum(oi.revenue_per_item)) * 100
            else 0
        end as profit_margin_percent,

        -- Average metrics
        avg(oi.profit_margin_percent) as avg_item_profit_margin_percent

    from order_items oi
    inner join orders o
        on oi.order_id = o.order_id
    group by
        oi.order_id,
        oi.customer_id,
        oi.location_id,
        oi.order_date,
        o.order_total,
        o.subtotal,
        o.tax_paid

)

select * from order_metrics

