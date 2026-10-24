# Доповнення до наявного prescriptions-service

Це НЕ повна заміна лабораторної №1. Пакет містить `apply_lab2.py`, `prometheus/prometheus.yml` і `REPORT2.md`.

1. Зберегти поточні зміни через GitHub Desktop або `git add .; git commit -m "Save Lab 1 before Lab 2"`.
2. Розпакувати ці три файли / папку всередину **наявної** `E:\UNI\VO\prescriptions-service`.
3. Відкрити PowerShell і перейти `cd E:\UNI\VO\prescriptions-service`.
4. Запустити `python .\apply_lab2.py` (створює резервні копії лише п'яти змінюваних файлів).
5. Перевірити `docker compose config`.
6. Запустити `docker compose up -d --build --scale web=1`.
7. Перевірити `docker compose ps`, `http://localhost:8080/livez`, `http://localhost:8080/healthz`, `http://localhost:8080/metrics`, `http://localhost:9090/targets`, `http://localhost:8080/noop` (204).

Зверніть увагу: скрипт додає метрики та моніторинг, але повне виконання методички (автоматичний генератор, USL, графіки, graceful-shutdown без помилок) вимагає окремих запусків і перевірок. Резервні копії `*.lab1-backup` зберігати локально, не комітити у Git.
