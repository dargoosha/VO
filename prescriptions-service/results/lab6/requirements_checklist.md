# Матриця вимог лабораторної 6

| Вимога методички | Реалізація | Команда перевірки | Первинні дані | Рисунок або таблиця | Розділ звіту |
|---|---|---|---|---|---|
| OCTMNIST, перші 7000 train, увесь test | `lab6/common.py`, перевірка NPZ у `lab6/run_experiments.py` | `python lab6/run_experiments.py` | `experiment_manifest.json` | Таблиця стенда | 2–3 |
| Мережа Ш3 без BatchNorm, 8 тензорів параметрів | `Net`, `n_params_formula` | `python -m unittest -v lab6.test_lab6` | `experiment_manifest.json` | Таблиця параметрів | 2 |
| Один процес, B=192, η₀=0,02, 3 епохи | `train.py --action train --mode single` | повний runbook | `raw/quality_single_b192.json` | Таблиця якості | 4 |
| 10 кроків прогріву і 50 вимірюваних кроків | `train.py --action speed` | повний runbook | `raw/speed_*.json`, `logs/*.log` | Таблиці синхронізації і scaling | 3–6 |
| Ручний broadcast і all-reduce на P=4 через loopback | `train.py --mode manual`, `GLOO_SOCKET_IFNAME=lo` | повний runbook | `raw/speed_manual_p4_b192.json` | Таблиця синхронізації | 4 |
| Еквівалентність градієнта на першому батчі | `gradient_equivalence` | аналіз `analysis_checks.json` | `raw/speed_manual_p4_b192.json` | Таблиця еквівалентності | 4 |
| Однакові ваги процесів після 50 кроків | `weight_sum_range` + `all_gather` | аналіз `analysis_checks.json` | `raw/speed_manual_p4_b192.json` | Таблиця еквівалентності | 4 |
| DDP і окремий no_sync | `--mode ddp`, `--mode ddp-no-sync` | повний runbook | `raw/speed_strong_p*.json` | `synchronization.csv` | 4 |
| Strong scaling P=1..4, B=192 | серія `speed_strong_*` | `python analyze_lab6.py` | `strong_scaling.csv` | `01_strong_scaling.png` | 5 |
| Weak scaling P=1..4, b=48 | серія `speed_weak_*`, P=4 із strong | `python analyze_lab6.py` | `weak_scaling.csv` | `02_weak_scaling.png` | 6 |
| Якість P=1,b=48; P=4,B=192,η₀; P=4,B=192,4η₀ | три quality-прогони | `python analyze_lab6.py` | `quality.csv`, відповідні `raw/*.json` | `04_quality.png` | 7 |
| Checkpoint K_c=20, відмова rank=3 на s_f=50 | `train.py --action fault`, статичний torchrun, generation-specific `PrefixStore`, 5 с перед повторною ініціалізацією | повний runbook | `raw/fault_ddp_p4_b192.json`, `logs/fault_*.log` | Таблиця відновлення | 8 |
| Порівняння loss 48..52 та ACC | `analyze_lab6.py` | `python analyze_lab6.py` | `fault_loss_comparison.csv`, `fault_summary.json` | `05_fault_recovery_loss.png` | 8 |

Статус кожної вимоги вважається підтвердженим лише після появи первинного файла,
успішного `analysis_checks.json` і звірки відповідного розділу `REPORT6.md`.

## Підсумок перевірки

- Усі 18 запланованих первинних JSON і відповідні console logs створено.
- `analysis_checks.json` має `passed=true`; усі вісім машинних перевірок успішні.
- П’ять графіків побудовано з CSV/JSON і візуально перевірено.
- Усі дев’ять таблиць методички заповнено у `REPORT6.md` фактичними даними.
- Робочий датасет і checkpoint-и ізольовано від Git; секрети не використовувалися.
