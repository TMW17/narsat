{# Every non-null value is greater than or equal to `minimum`. #}
{% test at_least(model, column_name, minimum) %}
select {{ column_name }}
from {{ model }}
where {{ column_name }} < {{ minimum }}
{% endtest %}
