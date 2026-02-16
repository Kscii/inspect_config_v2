启动数据库
cd db 
docker-compose up -d

初始化数据库
docker exec -i inspect_config_db psql -U inspect_user -d inspect_config_db < db/ddl.sql

同步presets配置
python -m src.main --sync-presets

运行数据收集
python -m src.main


