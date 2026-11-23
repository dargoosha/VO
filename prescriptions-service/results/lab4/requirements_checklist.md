# Матриця вимог лабораторної роботи 4

Варіант 6: `q_lo=0,01`, `q_hi=0,99`, `T_poll=100 мс`, `L=15 с`, `R=3`, Ф3, Д4.

| Вимога | Реалізація | Перевірка | Первинний доказ | Стан |
|---|---|---|---|---|
| tasks і task_results | `lab4/migration.sql` | migration Job | `results/lab4/schema.txt` | перевірено |
| API задач і gauges | `app/task_api.py` | HTTP 202/200/404, Prometheus | `results/lab4/smoke.json` | smoke пройдено |
| окремий worker | `worker/main.py`, `lab4/worker.yaml` | 20 задач, t_io/t_cpu | `step2_tasks.csv`, `step2_duration_prometheus.json` | виміряно |
| наївні дублі | `CLAIM_MODE=naive` | 300 задач, 100 RPS, 100/1 мс; таблиця і журнали | `step3_naive_*` | D=0,073333 і 0,076667; оцінки збіглися |
| SKIP LOCKED | `CLAIM_MODE=locked` | D, claim p99 | `step4_skip_locked_*` | D=0 |
| без SKIP LOCKED | `CLAIM_MODE=locked_no_skip` | locks, empty claims, p99 | `step4_no_skip_*` | виміряно; max lock waiters=0 |
| lease/heartbeat/fencing/idempotency | worker + PK | рівно 100 задач, pause/resume 30 с | `step5_*` | attempts/token 1→2, один результат, без штучної затримки |
| X(1), X(2), X(3), USL | сценарій capacity | 400 задач, інтервал непорожньої черги | `step6_*`, `analysis_summary.json` | X=11,921/23,502/34,005 задач/с; σ=0; κ=0,008486; W*=10,86 |
| закон Літтла | steady profile | 0,7 X(3), 180 с | `step7_steady_*` | розбіжність 6,54% |
| профіль 1,3 X(3) | профіль навантаження | 1,3 X(3), 120 с | `step7_overload_*` | queue=1583; Wq зріс 0,561→46,581 с |
| SIGTERM | signal handler і 25-секундний watchdog | rollout restart та unit-тест тайм-ауту | `step8_*`, `worker/test_shutdown_watchdog.py` | 2856 done, 0 failed/duplicates; три exit code=0; умовне повернення за token |
| FAIL_RATE=0,15 і Ф3 | env + poison seed | дві серії по 300 с при 0,7 X(3), Ф3 на 150-й с, повний drain | `step9_*`, `step9_theory_comparison.csv` | по 7141 задачі; 10 poison failed з attempts=3; exact p-value для f/f³: 0,3201/0,6823/0,7277/0,0513 |
| Д4 batch b=1/10 | CLAIM_BATCH_SIZE | active throughput, claim p99, SIGTERM returns | `step10_*` | X 34,005→33,598; p99 0,04049→0,03590 с; 0→11 returned |
