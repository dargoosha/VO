# Матриця вимог лабораторної роботи 4

Варіант 6: `q_lo=0,01`, `q_hi=0,99`, `T_poll=100 мс`, `L=15 с`, `R=3`, Ф3, Д4.

| Вимога | Реалізація | Перевірка | Первинний доказ | Стан |
|---|---|---|---|---|
| tasks і task_results | `lab4/migration.sql` | migration Job | `results/lab4/schema.txt` | перевірено |
| API задач і gauges | `app/task_api.py` | HTTP 202/200/404, Prometheus | `results/lab4/smoke.json` | smoke пройдено |
| окремий worker | `worker/main.py`, `lab4/worker.yaml` | 20 задач, t_io/t_cpu | `step2_tasks.csv`, `step2_duration_prometheus.json` | виміряно |
| наївні дублі | `CLAIM_MODE=naive` | 300 задач, 100 RPS, 100/1 мс | `step3_naive_*` | D=0,12 і 0,336667 |
| SKIP LOCKED | `CLAIM_MODE=locked` | D, claim p99 | `step4_skip_locked_*` | D=0 |
| без SKIP LOCKED | `CLAIM_MODE=locked_no_skip` | locks, empty claims, p99 | `step4_no_skip_*` | виміряно; max lock waiters=0 |
| lease/heartbeat/fencing/idempotency | worker + PK | рівно 100 задач, pause/resume 30 с | `step5_*` | attempts/token 1→2, один результат, без штучної затримки |
| X(1), X(2), X(3), USL | сценарій capacity | 400 задач, останні 30 с | `step6_*`, `analysis_summary.json` | X_30=10,033/13,333/13,333 задач/с |
| закон Літтла | steady profile | 0,7 X_30(3), 180 с | `step7_steady_*` | розбіжність 28,07% |
| профіль 1,3 X(3) | профіль навантаження | 1,3 X_30(3), 120 с | `step7_overload_*` | queue=0; заданий темп нижчий за активну місткість |
| SIGTERM | worker signal handler | rollout restart | `step8_*` | 1119 done, 0 failed/duplicates; три exit code=0 |
| FAIL_RATE=0,15 і Ф3 | env + poison seed | 300 с, Ф3 на 150-й с, повний drain | `step9_*`, `step9_theory_comparison.csv` | 10 poison failed з attempts=3; non-poison узгоджується з f і f³ |
| Д4 batch b=1/10 | CLAIM_BATCH_SIZE | X_30(3), active throughput, claim p99, SIGTERM returns | `step10_*` | X_30 без зміни; active 26,483→34,538; 0→11 returned |
