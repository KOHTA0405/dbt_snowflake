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
{% if [true, false] | random %}
-- Intentional ~50% random failure for testing PrefectDbtOrchestrator's
-- per-node error handling (this table does not exist).
cross join intentional_test_failure_nonexistent_table
{% endif %}

