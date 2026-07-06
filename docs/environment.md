# 环境准备

## Python

```bash
cd /home/pqb/codes/inspect_config
uv sync
```

## 本地配置

```bash
cd /home/pqb/codes/inspect_config
cp config.local.yaml.example config.local.yaml
```

## OBS 配置

```bash
mkdir -p /home/pqb/shanghai_config /home/pqb/zhengzhou_config
obsutil config -interactive -config=/home/pqb/shanghai_config/.obsutilconfig_shanghai
obsutil config -interactive -config=/home/pqb/zhengzhou_config/.obsutilconfig_zhengzhou
```

```bash
obsutil ls obs://openloong-apps-dev-private/data-collector-svc/collect/ -config=/home/pqb/shanghai_config/.obsutilconfig_shanghai -limit 5
obsutil ls obs://openloong-zhengzhou-apps-private/data-collector-svc/collect/ -config=/home/pqb/zhengzhou_config/.obsutilconfig_zhengzhou -limit 5
```

## 可视化 DB

```bash
cd /home/pqb/codes/inspect_config/src/visualize_db_app
docker compose up -d db
docker compose ps
```

```bash
docker exec -it inspect_pg psql -U openloong -d inspect
```

## 运行

```bash
cd /home/pqb/codes/inspect_config
uv run src/run.py
```
