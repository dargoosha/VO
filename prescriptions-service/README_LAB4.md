# Лабораторна робота 4

Runbook для PowerShell. Команди виконуються з кореня `prescriptions-service`.
Вони використовують наявний kind-кластер лабораторної 3 і не видаляють PVC.

## Розгортання і smoke test

```powershell
.\lab4\deploy.ps1
.\lab4\smoke.ps1
```

Очікуються Ready web/worker pods, HTTP 202 для створення задачі, стан `done`,
результат і worker target `UP` у Prometheus.

## Режими захоплення

```powershell
.\lab4\set-mode.ps1 -Mode naive -PollMs 100 -ResetTasks
.\lab4\set-mode.ps1 -Mode locked -PollMs 1 -ResetTasks
.\lab4\set-mode.ps1 -Mode locked_no_skip -PollMs 1 -ResetTasks
```

`-ResetTasks` видаляє лише експериментальні `tasks/task_results`; записи
prescriptions і PVC не видаляються.

## Основні сценарії

```powershell
.\lab4\run-step5.ps1
.\lab4\run-capacity.ps1 -BatchSize 1 -Prefix step6

$x3 = [double](Import-Csv .\results\lab4\step6_summary.csv |
    Where-Object workers -eq '3').X_30s
.\lab4\run-profiles.ps1 -X3 $x3
.\lab4\run-step8.ps1 -Rate (0.7 * $x3) -Prefix step8
.\lab4\run-capacity.ps1 -BatchSize 10 -Prefix step10_batch10 -Workers 3
```

`X_30s` — обов'язкове визначення пропускної здатності для профілів. Значення
`400 / active_seconds` у звіті є лише додатковим показником.

Крок 9 разом із повним drain триває близько 25 хв. Щоб наростаючий
CrashLoopBackOff не зупинив повторні спроби, сценарій тимчасово обмежує
максимальний backoff kubelet на двох worker-вузлах kind. Початкові файли
конфігурації зберігаються і відновлюються в `finally`:

```powershell
try {
    .\lab4\set-kubelet-backoff.ps1 -Mode Lab4
    .\lab4\run-step9.ps1 -Rate (0.7 * $x3) -Phase both
}
finally {
    .\lab4\set-kubelet-backoff.ps1 -Mode Restore
}
```

Не очищуйте `tasks` і `task_results` до завершення автоматичного аудиту. Під
час drain `FAIL_RATE=0,15` залишається активним.

## Аналіз

```powershell
python .\analyze_lab4.py
```

Скрипт читає лише фактичні файли `results/lab4/`, записує
`analysis_summary.json`, `capacity_summary.csv` і графіки `images/lab4/`.

## Безпечний базовий стан після експериментів

```powershell
.\lab4\set-mode.ps1 -Mode locked -BatchSize 1 -PollMs 100 -FailRate 0
kubectl -n prescriptions-lab3 scale deployment/worker --replicas=3
kubectl -n prescriptions-lab3 rollout status deployment/worker --timeout=180s
```
