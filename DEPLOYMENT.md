# Deployment Guide - Autonomous Browser Agent

This guide covers deploying the agent in production environments.

## 🚀 Quick Start

### 1. Environment Setup

```bash
# Install on Ubuntu VM
git clone <repository>
cd autonomous-browser-agent

# Run setup script
chmod +x setup.sh
./setup.sh

# Set API key
export OPENAI_API_KEY="your_api_key_here"
```

### 2. Verify Installation

```bash
# Test Python imports
python3 -c "import playwright; import openai; import bs4; print('✓ All imports successful')"

# Test browser installation
playwright show-trace --help

# Test proxy connection
curl --proxy http://127.0.0.1:7890 https://google.com
```

### 3. First Run

```bash
# Interactive mode
python main.py

# Or use examples
python examples.py
```

## 🔧 Production Configuration

### Environment Variables

Create a `.env` file or set these in your environment:

```bash
# Required
export OPENAI_API_KEY="sk-..."
export PROXY_URL="http://your-proxy:port"

# Optional
export API_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
export MODEL_NAME="glm-4"
export USER_DATA_DIR="/opt/browser-agent/data"
export HEADLESS="true"
export LOG_LEVEL="INFO"
```

### Systemd Service (Linux)

Create `/etc/systemd/system/browser-agent.service`:

```ini
[Unit]
Description=Autonomous Browser Agent
After=network.target

[Service]
Type=simple
User=agent
WorkingDirectory=/opt/browser-agent
Environment="OPENAI_API_KEY=your_key_here"
Environment="PROXY_URL=http://127.0.0.1:7890"
Environment="HEADLESS=true"
ExecStart=/usr/bin/python3 /opt/browser-agent/main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable browser-agent
sudo systemctl start browser-agent
sudo systemctl status browser-agent
```

## 🐳 Docker Deployment

### Dockerfile

```dockerfile
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application
COPY . .

# Create data directory
RUN mkdir -p /app/browser_data

# Set environment
ENV PYTHONUNBUFFERED=1

# Run agent
CMD ["python", "main.py"]
```

### Docker Compose

```yaml
version: '3.8'

services:
  browser-agent:
    build: .
    environment:
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - PROXY_URL=http://proxy:7890
      - HEADLESS=true
    volumes:
      - ./browser_data:/app/browser_data
      - ./logs:/app/logs
    depends_on:
      - proxy
    restart: unless-stopped

  proxy:
    image: your-proxy-image
    ports:
      - "7890:7890"
    restart: unless-stopped
```

Run with:

```bash
docker-compose up -d
docker-compose logs -f browser-agent
```

## 🔒 Security Best Practices

### 1. API Key Management

**Never commit API keys to git:**

```bash
# Use environment variables
export OPENAI_API_KEY="$(cat /secure/path/api_key.txt)"

# Or use secrets manager
aws secretsmanager get-secret-value --secret-id browser-agent-api-key
```

**Rotate keys regularly:**
- Set up key rotation schedule
- Use separate keys for dev/staging/prod
- Monitor usage for anomalies

### 2. Proxy Security

**Secure proxy access:**

```bash
# Use authenticated proxy
export PROXY_URL="http://username:password@proxy.internal:7890"

# Or use IP whitelisting on proxy server
```

**Monitor proxy traffic:**
- Log all requests
- Set up alerts for unusual patterns
- Rate limit per source IP

### 3. Browser Data Isolation

**Separate profiles for different tasks:**

```python
config = Config.from_env()
config.user_data_dir = f"/app/profiles/{task_id}"
```

**Regular cleanup:**

```bash
# Clean old browser data (keep last 7 days)
find /app/browser_data -type d -mtime +7 -exec rm -rf {} +
```

### 4. Resource Limits

**Set memory limits:**

```python
# In systemd service
MemoryLimit=2G
CPUQuota=150%
```

**In Docker:**

```yaml
services:
  browser-agent:
    mem_limit: 2g
    cpus: 1.5
```

## 📊 Monitoring and Logging

### Structured Logging

Update logging configuration in `main.py`:

```python
import logging.handlers

handler = logging.handlers.RotatingFileHandler(
    '/var/log/browser-agent/agent.log',
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5
)

formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - [%(task_id)s] - %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)
```

### Metrics Collection

Add metrics tracking:

```python
from prometheus_client import Counter, Histogram

task_counter = Counter('agent_tasks_total', 'Total tasks', ['status'])
task_duration = Histogram('agent_task_duration_seconds', 'Task duration')

@task_duration.time()
def run_task(task):
    try:
        result = agent.run(task)
        task_counter.labels(status='success').inc()
        return result
    except Exception:
        task_counter.labels(status='failed').inc()
        raise
```

### Health Checks

Add health check endpoint:

```python
from flask import Flask, jsonify

app = Flask(__name__)

@app.route('/health')
def health():
    return jsonify({
        'status': 'healthy',
        'browser': check_browser_status(),
        'llm': check_llm_connection(),
        'proxy': check_proxy_connection()
    })

if __name__ == '__main__':
    app.run(port=8080)
```

## 🔍 Troubleshooting

### Common Issues

**1. Browser won't start in Docker**

```dockerfile
# Add these to Dockerfile
RUN apt-get install -y \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxkbcommon0 \
    libgbm1 \
    libasound2
```

**2. Proxy timeouts**

```python
# Increase timeouts
config.page_load_timeout = 60000
config.action_timeout = 30000
```

**3. Memory leaks**

```python
# Restart browser periodically
class Agent:
    def __init__(self):
        self.task_count = 0
    
    def run(self, task):
        self.task_count += 1
        if self.task_count % 10 == 0:
            self.restart_browser()
```

### Debugging Tools

**Enable verbose logging:**

```bash
export LOG_LEVEL=DEBUG
python main.py
```

**Inspect browser state:**

```python
# Add breakpoint before action
import pdb; pdb.set_trace()

# Or save screenshot
self.browser.page.screenshot(path='debug.png')
```

**Record Playwright trace:**

```python
self.context.tracing.start(screenshots=True, snapshots=True)
# ... run task ...
self.context.tracing.stop(path='trace.zip')
```

## 📈 Performance Optimization

### 1. Reduce Context Size

```python
class DOMProcessor:
    def process_page(self, html):
        # Limit text content
        text_content = text[:1000]  # Only first 1000 chars
        
        # Limit interactive elements
        elements = elements[:50]  # Max 50 elements
```

### 2. Cache LLM Responses

```python
from functools import lru_cache

@lru_cache(maxsize=100)
def get_cached_decision(page_hash, task):
    return llm_client.chat(...)
```

### 3. Parallel Task Execution

```python
from concurrent.futures import ThreadPoolExecutor

def run_multiple_tasks(tasks):
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(run_task, task) for task in tasks]
        return [f.result() for f in futures]
```

### 4. Browser Pool

```python
class BrowserPool:
    def __init__(self, size=3):
        self.browsers = [BrowserManager() for _ in range(size)]
        self.queue = Queue()
    
    def get_browser(self):
        return self.queue.get()
    
    def return_browser(self, browser):
        self.queue.put(browser)
```

## 🧪 Testing in Production

### Canary Deployment

```bash
# Deploy to 10% of traffic
kubectl apply -f canary-deployment.yaml

# Monitor error rate
kubectl logs -f deployment/browser-agent-canary | grep ERROR

# If stable, promote to 100%
kubectl apply -f production-deployment.yaml
```

### A/B Testing

```python
def run_task(task, variant='A'):
    if variant == 'A':
        config.model_name = 'glm-4'
    else:
        config.model_name = 'glm-4-plus'
    
    agent = Agent(config)
    return agent.run(task)
```

## 📝 Maintenance Checklist

### Daily
- [ ] Check error logs
- [ ] Monitor API usage/costs
- [ ] Verify proxy connectivity

### Weekly
- [ ] Review task success rates
- [ ] Clean up old browser data
- [ ] Update rate limit quotas

### Monthly
- [ ] Update dependencies
- [ ] Review and optimize prompts
- [ ] Analyze and fix common failure patterns
- [ ] Rotate API keys

## 🆘 Support

For production issues:
1. Check logs: `/var/log/browser-agent/`
2. Review metrics: `http://localhost:9090/metrics`
3. Contact: support@yourcompany.com
