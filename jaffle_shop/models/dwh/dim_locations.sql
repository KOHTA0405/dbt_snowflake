with

locations as (

    select * from {{ ref('stg_locations') }}

),

final as (

    select
        {{ dbt_utils.generate_surrogate_key(['location_id']) }} as location_sk,
        location_id,
        location_name,
        tax_rate,
        opened_date

    from locations

)

select * from final

