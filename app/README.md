# Aidison Runtime

这是 Aidison 的唯一可运行工程：FastAPI 后端、Next.js 工作台、PostgreSQL 迁移、测试与生产部署配置均位于此目录。

完整项目说明、快速启动与部署边界请阅读仓库根目录的 [`README.md`](../README.md)。

```bash
cp .env.example .env
docker compose up --build -d
```

开发工作台默认位于 <http://localhost:3000>，API 健康检查为 <http://localhost:8000/health>。
