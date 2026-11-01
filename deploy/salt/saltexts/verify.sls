{#- SKETCH. Proves the expected extension modules are loaded (a module whose extension or driver is missing is not). #}
{%- set want = salt['pillar.get']('saltexts:modules', []) %}
{%- set have = salt['sys.list_modules']() %}
{%- for m in want if m not in have %}
saltexts_missing_{{ m }}:
  test.fail_without_changes:
    - name: "module '{{ m }}' is not loaded (extension or driver missing, or restart pending)"
{%- endfor %}
saltexts_verified:
  test.succeed_without_changes:
    - name: all expected saltext modules are loaded
