with

customers as (

    select * from {{ ref('stg_customers') }}

),

final as (

    select
        {{ dbt_utils.generate_surrogate_key(['customer_id']) }} as customer_sk,
        customer_id,
        customer_name

    from customers

)

select * from final

