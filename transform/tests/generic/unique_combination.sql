{# The combination of columns identifies a row. Fails with one row per duplicated key. #}
{% test unique_combination(model, columns) %}
select
    {{ columns | join(', ') }},
    count(*) as n_rows
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}
