"""Non-destructive Lab 2 patch for existing prescriptions-service (Lab 1).
Run from prescriptions-service root: python path/to/apply_lab2.py
Backups are made beside changed files with .lab1-backup suffix.
"""
from pathlib import Path
import sys
root=Path.cwd()
required=['app/main.py','docker-compose.yml','Dockerfile','requirements.txt','nginx/nginx.conf','db/init.sql','REPORT.md']
missing=[f for f in required if not (root/f).exists()]
if missing: sys.exit('Wrong working directory. Missing: '+', '.join(missing))

def change(name, callback):
    p=root/name
    old=p.read_text(encoding='utf-8')
    new=callback(old)
    if new==old:
        print('Already patched:',name)
        return
    backup=p.with_name(p.name+'.lab1-backup')
    if not backup.exists(): backup.write_text(old,encoding='utf-8')
    p.write_text(new,encoding='utf-8')
    print('Updated:',name,'(backup:',backup.name+')')

def exact(s,old,new):
    if old not in s: raise ValueError('Cannot find expected original fragment: '+old[:100])
    return s.replace(old,new,1)

def main(s):
    if 'HTTP_REQUESTS_TOTAL = Counter(' in s:return s
    s=exact(s,'import os\n','import os\nimport socket\nimport time\n')
    s=exact(s,'from fastapi.responses import JSONResponse','from fastapi.responses import JSONResponse, Response\nfrom prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST')
    s=exact(s,'    pool_size=5,','    pool_size=int(os.getenv("DB_POOL_SIZE", "15")),')
    marker='@app.exception_handler(RequestValidationError)'
    metrics='''# Lab 2: metrics use route templates, never literal patient IDs.
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "Completed HTTP requests", ["method", "path", "code"]
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds", "Server-side HTTP processing duration",
    ["method", "path"], buckets=(.002,.005,.01,.02,.05,.1,.2,.5,1,2)
)
HTTP_IN_FLIGHT = Gauge("http_requests_in_progress", "Requests in progress")
POD_NAME = os.getenv("POD_NAME") or socket.gethostname()

@app.middleware("http")
async def lab2_metrics(request: Request, call_next):
    started = time.perf_counter()
    HTTP_IN_FLIGHT.inc()
    code = 500
    try:
        response = await call_next(request)
        code = response.status_code
        response.headers["X-Pod"] = POD_NAME
        return response
    finally:
        route = request.scope.get("route")
        path = getattr(route, "path", "__unmatched__")
        if request.url.path != "/metrics":
            HTTP_REQUESTS_TOTAL.labels(request.method, path, str(code)).inc()
            HTTP_REQUEST_DURATION.labels(request.method, path).observe(time.perf_counter()-started)
        HTTP_IN_FLIGHT.dec()

@app.get("/metrics", include_in_schema=False)
def metrics_endpoint():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST, headers={"X-Pod": POD_NAME})

@app.get("/livez")
def livez():
    return {"status": "alive"}


'''
    s=exact(s,marker,metrics+marker)
    return s

def deps(s):
    if 'prometheus-client' in s:return s
    return s.rstrip()+'\nprometheus-client>=0.21,<1\n'

def docker(s):
    if '--timeout-graceful-shutdown' in s:return s
    return exact(s,'"--host", "0.0.0.0", "--port", "8000"]','"--host", "0.0.0.0", "--port", "8000",\n     "--timeout-graceful-shutdown", "25"]')

def nginx(s):
    if 'location = /noop' in s:return s
    return exact(s,'        location / {','        location = /noop {\n            return 204;\n        }\n\n        location / {')

def compose(s):
    if '  prometheus:' in s:return s
    s=exact(s,'      DB_HOST: db','      DB_HOST: db\n      DB_POOL_SIZE: "15"')
    s=exact(s,'    mem_limit: 512m\n    networks:', '    mem_limit: 512m\n    stop_grace_period: 35s\n    networks:')
    s=exact(s,'volumes:\n  postgres_data:', '''  prometheus:
    image: prom/prometheus:v3.2.1
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus_data:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
    networks:
      - internal

volumes:
  prometheus_data:
  postgres_data:''')
    return s

# Validate all transformations before writing anything.
transforms={'app/main.py':main,'requirements.txt':deps,'Dockerfile':docker,'nginx/nginx.conf':nginx,'docker-compose.yml':compose}
try:
    for filename,fn in transforms.items():fn((root/filename).read_text(encoding='utf-8'))
except Exception as e:sys.exit('No files modified: '+str(e))
for filename,fn in transforms.items():change(filename,fn)
print('Existing REPORT.md, db/init.sql and seed.py were not modified.')
print('Next: docker compose config; docker compose up -d --build --scale web=1')
