# OC设定工作台 - 实施计划

## MVP范围: 暂存区画布 + AI产婆式对话

基于讨论确定的核心架构,本计划聚焦于最具创新性的功能:AI驱动的产婆式世界观构建工作台。

## 第一阶段:基础架构搭建 (Week 1-2)

### 1.1 后端基础 (Django)

**目标**: 搭建Django项目骨架,配置数据库和基础API

**任务清单**:
- [ ] 使用Django 4.2创建项目结构 `projectoc/`
- [ ] 配置PostgreSQL数据库连接
- [ ] 创建核心Django apps:
  - `core` - 核心数据模型
  - `ai_agent` - AI对话引擎
  - `canvas` - 暂存区管理
- [ ] 设置Django REST Framework
- [ ] 配置CORS和WebSocket支持(Django Channels)
- [ ] 创建基础数据模型:
  ```python
  # core/models/entity.py
  - Entity (统一实体表)
  - Relation (关系表)
  
  # canvas/models/staging.py
  - StagingCanvas (暂存区)
  - AIDialogue (对话历史)
  ```
- [ ] 编写数据库迁移文件并执行
- [ ] 创建基础API端点:
  - `/api/entities/` - CRUD
  - `/api/canvas/` - 画布管理
  - `/api/dialogue/` - 对话接口

**交付物**:
- 可运行的Django服务器
- 基础API文档(DRF自动生成)
- PostgreSQL数据库Schema

### 1.2 前端基础 (Vue 3)

**目标**: 搭建Vue 3 + TypeScript项目,配置核心依赖

**任务清单**:
- [ ] 使用Vite创建Vue 3 + TypeScript项目
- [ ] 安装核心依赖:
  ```bash
  npm install @tldraw/tldraw cytoscape @tiptap/vue-3 @tiptap/starter-kit
  npm install pinia vue-router axios element-plus
  npm install -D tailwindcss @types/cytoscape
  ```
- [ ] 配置Tailwind CSS
- [ ] 创建项目目录结构(按照FRONTEND_STACK.md)
- [ ] 配置Vue Router基础路由:
  - `/` - 工作台总览
  - `/canvas` - 暂存区画布
  - `/graph` - 知识图谱
- [ ] 创建Pinia stores骨架:
  - `canvasStore` - 画布状态
  - `dialogueStore` - 对话状态
  - `graphStore` - 图谱状态
- [ ] 配置Axios API客户端

**交付物**:
- 可运行的Vue开发服务器
- 基础路由和导航
- TypeScript类型定义文件

### 1.3 Docker Compose环境

**目标**: 一键启动开发环境

**任务清单**:
- [ ] 编写 `docker-compose.yml`:
  ```yaml
  services:
    postgres:
      image: postgres:16
      environment:
        POSTGRES_DB: projectoc
        POSTGRES_USER: ocuser
        POSTGRES_PASSWORD: ocpass
      volumes:
        - postgres_data:/var/lib/postgresql/data
      ports:
        - "5432:5432"
    
    redis:
      image: redis:7-alpine
      ports:
        - "6379:6379"
    
    backend:
      build: ./backend
      command: python manage.py runserver 0.0.0.0:8000
      volumes:
        - ./backend:/app
        - world_repos:/app/world_repos
      ports:
        - "8000:8000"
      depends_on:
        - postgres
        - redis
      environment:
        DATABASE_URL: postgresql://ocuser:ocpass@postgres:5432/projectoc
        REDIS_URL: redis://redis:6379/0
    
    frontend:
      build: ./frontend
      command: npm run dev -- --host 0.0.0.0
      volumes:
        - ./frontend:/app
        - /app/node_modules
      ports:
        - "5173:5173"
      depends_on:
        - backend
  
  volumes:
    postgres_data:
    world_repos:
  ```
- [ ] 编写 `backend/Dockerfile`
- [ ] 编写 `frontend/Dockerfile`
- [ ] 创建启动脚本 `start.sh`

**交付物**:
- 可用的Docker Compose配置
- 一键启动开发环境

## 第二阶段:暂存区画布实现 (Week 3-4)

### 2.1 TLDraw无限画布集成

**目标**: 实现可以绘图、写文字、贴便签的无限画布

**任务清单**:
- [ ] 创建 `TLDrawCanvas.vue` 组件
- [ ] 集成TLDraw基础功能:
  - 绘图工具(铅笔、矩形、圆形、箭头)
  - 文本工具
  - 平移和缩放
- [ ] 实现画布状态保存:
  ```typescript
  // 实时保存到后端
  watch(() => editor.getSnapshot(), 
    debounce((snapshot) => {
      canvasStore.saveSnapshot(snapshot)
    }, 1000)
  )
  ```
- [ ] 实现多用户协同(WebSocket同步)
- [ ] 添加撤销/重做功能

**交付物**:
- 可用的无限画布组件
- 实时保存功能

### 2.2 自定义画布元素

**目标**: 在TLDraw基础上添加特殊元素(贴纸、思维导图、实体草稿)

**任务清单**:
- [ ] 实现游离设定贴纸 `StickyNote.vue`:
  - 可编辑的便签
  - 颜色标签
  - 链接到实体的能力
- [ ] 实现思维导图节点:
  - 树状结构
  - 可展开/折叠
  - 节点可链接实体
- [ ] 实现实体草稿卡片:
  - 显示AI建议的实体
  - 包含标题、类型、属性
  - 显示建议的关联(虚线连接)
  - 确认/拒绝按钮
- [ ] 集成LaTeX公式支持:
  - 使用KaTeX渲染
  - 编辑模式和预览模式
- [ ] 集成Mermaid图表

**交付物**:
- 自定义画布元素组件集
- 元素数据模型

### 2.3 画布与知识图谱的桥接

**目标**: 画布中的草稿实体可以一键提交到知识图谱

**任务清单**:
- [ ] 实现"提取实体"功能:
  - 扫描画布中的草稿元素
  - 生成 `derivedEntities` 和 `derivedRelations`
- [ ] 实现提交流程:
  ```python
  # canvas/services/commit_service.py
  class CanvasCommitService:
      def commit_to_graph(self, canvas_id):
          # 1. 验证实体数据
          # 2. 创建Entity记录
          # 3. 创建Relation记录
          # 4. 标记画布为已提交
          # 5. 通过WebSocket通知前端更新图谱
  ```
- [ ] 实现批量审核界面:
  - 侧边栏显示所有待提交实体
  - 可以编辑、删除、确认
  - 一键全部提交

**交付物**:
- 画布到图谱的提交功能
- 批量审核界面

## 第三阶段:AI产婆式对话引擎 (Week 5-6)

### 3.1 OpenAI API集成

**目标**: 兼容OpenAI API,支持多个上游模型

**任务清单**:
- [ ] 创建AI服务抽象层:
  ```python
  # ai_agent/services/llm_service.py
  class LLMService:
      def __init__(self, base_url: str, api_key: str):
          self.client = OpenAI(base_url=base_url, api_key=api_key)
      
      def list_models(self):
          """获取上游可用模型列表"""
          return self.client.models.list()
      
      def chat_completion(self, messages, model, tools=None):
          """聊天完成"""
          return self.client.chat.completions.create(
              model=model,
              messages=messages,
              tools=tools
          )
  ```
- [ ] 实现模型配置管理:
  - 环境变量配置API base URL和key
  - 前端UI选择模型
  - 保存用户偏好模型
- [ ] 实现Function Calling for结构化提取:
  ```python
  tools = [
      {
          "type": "function",
          "function": {
              "name": "extract_entities",
              "description": "从用户描述中提取实体和关系",
              "parameters": {
                  "type": "object",
                  "properties": {
                      "entities": {"type": "array", ...},
                      "relations": {"type": "array", ...}
                  }
              }
          }
      }
  ]
  ```

**交付物**:
- OpenAI API兼容层
- 模型选择UI
- 结构化提取功能

### 3.2 苏格拉底式对话引擎

**目标**: AI能主动提问、挑战矛盾、发现空白

**任务清单**:
- [ ] 实现对话意图分析:
  ```python
  # ai_agent/services/dialogue_manager.py
  class DialogueManager:
      def analyze_intent(self, user_message, context):
          """分析用户意图: create/explore/deepen"""
          prompt = f"""
          分析用户的意图:
          用户消息: {user_message}
          当前上下文: {context}
          
          返回: {{"intent": "create|explore|deepen", "entities": [...], "relations": [...]}}
          """
          return self.llm.chat_completion(...)
  ```
- [ ] 实现矛盾检测:
  ```python
  def detect_inconsistencies(self, new_statement, existing_entities):
      """检测新陈述与已有设定的矛盾"""
      # 使用向量相似度找到相关实体
      # 用LLM判断是否矛盾
      prompt = f"""
      新陈述: {new_statement}
      已有设定: {existing_entities}
      
      判断是否存在逻辑矛盾,如果有,返回具体矛盾点。
      """
  ```
- [ ] 实现知识空白分析:
  ```python
  def find_knowledge_gaps(self, current_canvas):
      """发现重要的知识空白"""
      # 分析画布中的实体
      # 找出缺失的关键信息
      # 判断重要性
  ```
- [ ] 实现问题生成器:
  ```python
  QUESTION_TEMPLATES = {
      'fundamental': "让我们从基础开始。{entity}的本质是什么?",
      'challenging': "我注意到{conflict}。你如何解释这个矛盾?",
      'deepening': "关于{entity}的{aspect},能详细说说吗?",
      'connecting': "我看到{entity1}和{entity2},它们之间有什么关系?",
      'expanding': "{entity}还有其他方面需要考虑吗?"
  }
  ```
- [ ] 实现主动提问触发器:
  ```python
  class ProactiveDialogueTrigger:
      def should_trigger(self, canvas):
          # 条件1: 检测到矛盾
          # 条件2: 发现重要空白
          # 条件3: 实体过于孤立
          # 条件4: 适合深化的时机
  ```

**交付物**:
- 完整的产婆式对话引擎
- 主动提问功能

### 3.3 AI建议与可视化

**目标**: AI的建议在画布上实时可视化

**任务清单**:
- [ ] 实现建议渲染:
  - AI提取的实体自动生成草稿卡片
  - 建议的关联显示为虚线
  - 矛盾实体高亮显示
- [ ] 实现链接建议:
  ```python
  def suggest_links(self, new_entity, existing_entities):
      """为新实体建议关联"""
      # 使用向量相似度
      similar = self.find_similar(new_entity)
      
      # 用LLM推断关系类型
      for entity in similar:
          relation_type = self.infer_relation_type(new_entity, entity)
          yield {
              'target': entity,
              'relation_type': relation_type,
              'confidence': confidence,
              'reason': reason
          }
  ```
- [ ] 实现冲突警告UI:
  - 侧边栏显示检测到的矛盾
  - 点击可跳转到相关实体
  - 提供解决建议

**交付物**:
- AI建议可视化
- 链接建议功能
- 冲突警告UI

## 第四阶段:知识图谱基础 (Week 7-8)

### 4.1 基础图谱可视化

**目标**: 用Cytoscape.js展示提交后的实体和关系

**任务清单**:
- [ ] 创建 `CytoscapeGraph.vue` 组件
- [ ] 实现图谱布局:
  - Force-directed layout (力导向)
  - Hierarchical layout (层次)
  - Circular layout (环形)
- [ ] 实现节点样式:
  - 不同实体类型不同颜色和形状
  - 节点大小反映重要性
- [ ] 实现交互:
  - 点击节点显示详情
  - 悬浮高亮连接
  - 双向链接加粗显示
- [ ] 实现图谱过滤:
  - 按实体类型过滤
  - 按关系类型过滤
  - 搜索节点

**交付物**:
- 可用的知识图谱组件
- 基础交互功能

### 4.2 双向索引和反向链接

**目标**: Obsidian风格的双向链接

**任务清单**:
- [ ] 实现反向链接查询:
  ```python
  # core/services/graph_service.py
  def get_backlinks(self, entity_id):
      """获取指向该实体的所有链接"""
      return Relation.objects.filter(target_id=entity_id)
  ```
- [ ] 在实体详情页显示:
  - 出链(Outgoing links)
  - 入链(Incoming links/Backlinks)
- [ ] 实现链接面板:
  - 侧边栏显示所有链接
  - 分组显示(按关系类型)
  - 点击跳转

**交付物**:
- 双向链接查询API
- 反向链接UI

## 第五阶段:集成与优化 (Week 9-10)

### 5.1 WebSocket实时同步

**目标**: 多用户协同编辑

**任务清单**:
- [ ] 配置Django Channels
- [ ] 实现WebSocket consumer:
  ```python
  # canvas/consumers.py
  class CanvasConsumer(AsyncWebsocketConsumer):
      async def connect(self):
          self.canvas_id = self.scope['url_route']['kwargs']['canvas_id']
          await self.channel_layer.group_add(...)
      
      async def canvas_update(self, event):
          # 广播画布更新
  ```
- [ ] 前端WebSocket集成:
  ```typescript
  // composables/useWebSocket.ts
  const ws = new WebSocket(`ws://localhost:8000/ws/canvas/${canvasId}`)
  
  ws.onmessage = (event) => {
      const data = JSON.parse(event.data)
      if (data.type === 'canvas_update') {
          canvasStore.syncCanvas(data.canvas)
      }
  }
  ```
- [ ] 实现协同光标和选择高亮

**交付物**:
- 实时同步功能
- 协同编辑体验

### 5.2 性能优化

**任务清单**:
- [ ] 后端优化:
  - 数据库查询优化(select_related, prefetch_related)
  - 添加数据库索引
  - API响应缓存(Redis)
- [ ] 前端优化:
  - 图谱虚拟化(只渲染可见节点)
  - 懒加载实体详情
  - 防抖保存画布状态
- [ ] 大规模数据测试:
  - 1000+实体的图谱性能
  - 复杂画布的保存速度

**交付物**:
- 性能基准测试报告
- 优化后的系统

### 5.3 用户体验打磨

**任务清单**:
- [ ] 添加引导教程:
  - 首次使用引导
  - 功能提示
- [ ] 键盘快捷键:
  - 保存: Ctrl+S
  - 撤销/重做: Ctrl+Z / Ctrl+Y
  - 搜索: Ctrl+K
- [ ] 响应式设计:
  - 适配不同屏幕尺寸
  - 移动端基础支持
- [ ] 错误处理和提示:
  - 友好的错误消息
  - 网络断线提示
  - 自动重连

**交付物**:
- 完整的用户体验
- 使用文档

## 后续路线图 (MVP之后)

### Phase 2: Git版本控制
- Git仓库初始化和管理
- 版本历史和diff查看
- 分支和合并

### Phase 3: 时间线系统
- 多时间体系支持
- 时间切片查询
- 关系演变可视化
- 人物生命周期

### Phase 4: Neo4j图数据库
- 复杂图查询优化
- 路径发现算法
- 影响力分析

### Phase 5: 高级功能
- 导出为Markdown文档
- 设定冲突自动检测
- 世界观一致性评分
- 多人协作权限管理

## 技术债务和注意事项

### 安全性
- [ ] API认证和授权(JWT)
- [ ] 输入验证和XSS防护
- [ ] SQL注入防护
- [ ] 限流和防滥用

### 测试
- [ ] 后端单元测试(pytest)
- [ ] 前端单元测试(Vitest)
- [ ] E2E测试(Playwright)
- [ ] API测试(Postman/REST Client)

### 文档
- [ ] API文档(Swagger/OpenAPI)
- [ ] 用户使用手册
- [ ] 开发者文档
- [ ] 部署指南

## 估算工作量

| 阶段 | 工作量 | 依赖 |
|------|--------|------|
| 阶段1: 基础架构 | 2周 | 无 |
| 阶段2: 暂存区画布 | 2周 | 阶段1 |
| 阶段3: AI对话引擎 | 2周 | 阶段1 |
| 阶段4: 知识图谱 | 2周 | 阶段2,3 |
| 阶段5: 集成优化 | 2周 | 阶段2,3,4 |
| **总计** | **10周** | |

## 下一步行动

立即可以开始的任务:
1. 初始化Django项目
2. 初始化Vue 3项目
3. 编写docker-compose.yml
4. 创建数据库Schema设计

