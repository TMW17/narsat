{# A ratio (IoU, precision, recall): null is allowed, anything outside [0, 1] is not. #}
{% test between_zero_and_one(model, column_name) %}
select {{ column_name }}
from {{ model }}
where {{ column_name }} < 0 or {{ column_name }} > 1
{% endtest %}
