Контейнеризований REST API для роботи з призначеннями препаратів, реалізований за допомогою FastAPI, PostgreSQL та Nginx. 

**Підготовка середовища**

Для запуску потрібні Docker Desktop і Docker Compose.
Перейдіть до каталогу prescriptions-service та створіть файл .env на основі шаблону.

**PowerShell:**

    Copy-Item .env.example .env

У файлі .env задайте значення POSTGRES_USER, POSTGRES_PASSWORD і POSTGRES_DB. Пароль замініть на власний. Файл .env не слід додавати до Git.

Запустити 
```powershell
docker compose up -d --build --scale web=1
```

Після запуску API доступний за адресою: `http://localhost:8080/docs`

Перевірка готовності: curl.exe `http://localhost:8080/healthz`

Для запуску двох вебінстансів: 
```powershell
docker compose up -d --scale web=2
```

Для перегляду стану контейнерів:
`docker compose ps`

Для зупинки сервісів:
`docker compose down`

Дані PostgreSQL зберігаються в Docker-томі `postgres_data`