# OC设定工作台 - 系统设计文档

## 项目概述
基于Django + Vue.js的OC(Original Character)世界观设定管理系统，融合知识图谱、版本控制和AI对话式构建。

## 核心概念模型

### 1. 设定系统 (Setting System)
```
设定系 (Canonical Setting)
├── Git版本管理
├── 严谨的学科式结构
├── 树状分支扩展
└── 环环相扣的逻辑链

游离设定 (Floating Tip)
├── 独立碎片
├── 通过Link关联
└── 灵活附着
```

### 2. 实体类型 (Entity Types)

#### A. 设定系实体 (Canonical Entities)
- 世界观设定 (World Settings)
- 势力/组织 (Factions)
- 地理/位置 (Locations)
- 魔法/科技体系 (Systems)
- 物品/道具 (Items)

#### B. 人物实体 (Character Entities)
- 基础信息
- 生命周期时间线
- 关系图谱 (时间切片)
- 归属设定系

#### C. 时间线实体 (Timeline Entities)
- 事件节点
- 时间尺度
- 故事脉络
- 人物关系演变

### 3. 关系类型

```python
# 双向索引关系
RELATION_TYPES = {
    'DERIVES_FROM': '衍生自',      # 设定系分支
    'BELONGS_TO': '归属于',        # 实体所属
    'LINKED_TO': '关联到',         # 游离设定链接
    'INFLUENCES': '影响',          # 因果关系
    'CONFLICTS_WITH': '冲突于',    # 矛盾设定
    'EVOLVES_TO': '演变为',        # 时间线演化
    'KNOWS': '认识',               # 人物关系
    'LOCATED_AT': '位于',          # 地理关系
    'OWNS': '拥有',                # 所有权
    'PARTICIPATES_IN': '参与',     # 事件参与
}
```

## 技术架构

### 后端 (Django)

```
projectoc/
├── manage.py
├── config/
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── core/              # 核心模型
│   │   ├── models/
│   │   │   ├── base.py
│   │   │   ├── setting.py
│   │   │   ├── character.py
│   │   │   ├── timeline.py
│   │   │   └── relation.py
│   │   ├── services/
│   │   │   ├── graph_service.py
│   │   │   ├── version_service.py
│   │   │   └── timeline_service.py
│   │   └── api/
│   ├── knowledge_graph/    # 知识图谱引擎
│   │   ├── neo4j_adapter.py
│   │   ├── graph_builder.py
│   │   └── query_engine.py
│   ├── version_control/    # 版本管理
│   │   ├── git_service.py
│   │   ├── diff_engine.py
│   │   └── merge_resolver.py
│   ├── ai_agent/           # AI对话代理
│   │   ├── dialogue_manager.py
│   │   ├── world_builder.py
│   │   └── suggestion_engine.py
│   └── export/             # 导出功能
│       ├── markdown_exporter.py
│       └── visualization.py
└── requirements.txt
```

### 前端 (Vue.js)

```
frontend/
├── src/
│   ├── views/
│   │   ├── GraphView.vue        # 图谱视图
│   │   ├── EditorView.vue       # 编辑器
│   │   ├── TimelineView.vue     # 时间线视图
│   │   ├── CharacterView.vue    # 人物视图
│   │   └── ChatView.vue         # AI对话
│   ├── components/
│   │   ├── graph/
│   │   │   ├── ForceGraph.vue   # 力导向图
│   │   │   ├── TreeGraph.vue    # 树状图
│   │   │   └── TimeSlice.vue    # 时间切片
│   │   ├── editor/
│   │   │   ├── MarkdownEditor.vue
│   │   │   ├── LinkSuggestion.vue
│   │   │   └── VersionHistory.vue
│   │   └── ai/
│   │       ├── ChatInterface.vue
│   │       └── SuggestionPanel.vue
│   ├── stores/
│   │   ├── graph.js
│   │   ├── settings.js
│   │   └── timeline.js
│   └── utils/
│       ├── graph-layout.js
│       └── api.js
└── package.json
```

## 数据库设计

### PostgreSQL (关系数据)

```sql
-- 基础实体表
CREATE TABLE entities (
    id UUID PRIMARY KEY,
    type VARCHAR(50),  -- 'canonical_setting', 'character', 'timeline', 'item', etc.
    title VARCHAR(255),
    content TEXT,
    metadata JSONB,
    git_repo_path VARCHAR(255),  -- 对于设定系实体
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    version_hash VARCHAR(64)
);

-- 关系表
CREATE TABLE relations (
    id UUID PRIMARY KEY,
    source_id UUID REFERENCES entities(id),
    target_id UUID REFERENCES entities(id),
    relation_type VARCHAR(50),
    weight FLOAT,  -- 关系强度
    properties JSONB,
    valid_from TIMESTAMP,  -- 时间有效性
    valid_to TIMESTAMP,
    created_at TIMESTAMP
);

-- 时间线事件表
CREATE TABLE timeline_events (
    id UUID PRIMARY KEY,
    timeline_id UUID REFERENCES entities(id),
    title VARCHAR(255),
    description TEXT,
    timestamp BIGINT,  -- 世界内时间
    real_timestamp TIMESTAMP,  -- 创作时间
    importance INT,  -- 重要程度(质点)
    participants JSONB,  -- 参与角色
    location_id UUID
);

-- 人物生命周期表
CREATE TABLE character_lifecycle (
    id UUID PRIMARY KEY,
    character_id UUID REFERENCES entities(id),
    event_type VARCHAR(50),  -- 'birth', 'death', 'transformation', etc.
    timestamp BIGINT,
    description TEXT,
    related_events UUID[]
);

-- 版本历史表
CREATE TABLE version_history (
    id UUID PRIMARY KEY,
    entity_id UUID REFERENCES entities(id),
    commit_hash VARCHAR(64),
    author VARCHAR(100),
    message TEXT,
    changes JSONB,
    created_at TIMESTAMP
);
```

### Neo4j (图数据库)

```cypher
// 节点类型
(:CanonicalSetting {id, title, content, branch_path})
(:Character {id, name, lifecycle_stages})
(:Timeline {id, name, scale})
(:Event {id, title, timestamp, importance})
(:Item {id, name, properties})
(:Location {id, name, coordinates})
(:FloatingTip {id, content, tags})

// 关系类型 (前面定义的RELATION_TYPES)
// 每个关系都带有时间有效性和属性
```

## 核心功能设计

### 1. 双向索引与图谱

```python
# apps/knowledge_graph/graph_builder.py
class GraphBuilder:
    def build_graph(self, entities, relations):
        """构建实时知识图谱"""
        pass
    
    def get_bidirectional_links(self, entity_id):
        """获取双向链接"""
        pass
    
    def calculate_influence_paths(self, source, target):
        """计算影响路径"""
        pass
    
    def detect_conflicts(self, entity_id):
        """检测设定冲突"""
        pass
```

### 2. 设定系版本管理

```python
# apps/version_control/git_service.py
class SettingVersionService:
    def init_setting_repo(self, setting_id):
        """初始化设定系Git仓库"""
        pass
    
    def create_branch(self, setting_id, branch_name):
        """创建设定分支"""
        pass
    
    def commit_changes(self, setting_id, changes, message):
        """提交变更"""
        pass
    
    def merge_branches(self, setting_id, source, target):
        """合并分支(处理设定冲突)"""
        pass
    
    def get_diff(self, setting_id, commit1, commit2):
        """获取差异"""
        pass
```

### 3. 时间切片与关系图

```python
# apps/core/services/timeline_service.py
class TimelineService:
    def get_time_slice(self, timestamp):
        """获取某个时间点的状态切片"""
        relations = self.get_active_relations(timestamp)
        characters = self.get_alive_characters(timestamp)
        return self.build_relation_graph(characters, relations)
    
    def get_character_timeline(self, character_id):
        """获取角色完整时间线"""
        pass
    
    def interpolate_relationships(self, char1, char2, time_range):
        """插值计算关系演变"""
        pass
```

### 4. AI对话式构建

```python
# apps/ai_agent/world_builder.py
class SocraticWorldBuilder:
    """产婆式世界观构建"""
    
    def start_dialogue(self, topic):
        """开始对话"""
        return self.ask_fundamental_question(topic)
    
    def process_answer(self, answer, context):
        """处理回答并追问"""
        # 分析回答
        entities = self.extract_entities(answer)
        relations = self.infer_relations(answer, context)
        
        # 检测不一致性
        conflicts = self.detect_inconsistencies(entities, context.world)
        
        if conflicts:
            return self.challenge_inconsistency(conflicts)
        
        # 探索深度
        return self.ask_deepening_question(entities, context)
    
    def suggest_expansion(self, current_setting):
        """建议扩展方向"""
        gaps = self.analyze_gaps(current_setting)
        return self.generate_questions(gaps)
    
    def auto_link_suggestions(self, new_entity):
        """自动建议关联"""
        similar = self.find_similar_entities(new_entity)
        return self.suggest_links(similar, new_entity)
```

## 前端交互设计

### 1. 图谱视图模式

```javascript
// 力导向图 - 展示整体知识网络
// 树状图 - 展示设定系分支结构  
// 时间线图 - 展示事件序列
// 关系图 - 展示人物关系(时间切片)
```

### 2. 编辑器特性

- Markdown编辑 + [[wikilink]]语法
- 实时链接建议
- 内联AI对话
- 版本历史对比
- 冲突高亮

### 3. 时间切片器

```vue
<!-- TimeSlice.vue -->
<template>
  <div class="time-slicer">
    <timeline-slider 
      :events="events"
      v-model="currentTime"
      @change="updateGraph"
    />
    <relation-graph 
      :nodes="characters"
      :edges="relations"
      :time="currentTime"
    />
  </div>
</template>
```

## 技术栈

### 后端
- Django 4.2+
- Django REST Framework
- Neo4j (图数据库)
- PostgreSQL (关系数据)
- GitPython (版本控制)
- OpenAI API / Anthropic Claude (AI对话)
- Celery (异步任务)

### 前端
- Vue 3 + Composition API
- Pinia (状态管理)
- D3.js / Cytoscape.js (图可视化)
- TipTap (富文本编辑)
- Vite (构建工具)

### 部署
- Docker + Docker Compose
- Nginx
- Redis (缓存)

## 下一步行动

1. 搭建基础Django项目结构
2. 设计详细的数据模型
3. 实现核心图谱引擎
4. 开发Vue前端原型
5. 集成AI对话功能
6. 实现版本控制系统
7. 开发时间线功能

## 示例使用场景

```
用户: "我想创建一个魔法体系"
AI: "让我们从基础开始。这个世界的魔法能量来源是什么?"
用户: "来自星辰的辐射"
AI: [创建实体: 星辰魔法体系]
    "有趣。那么不同时间、不同地点的星辰位置会影响魔法吗?"
用户: "会的,白天魔法会减弱"
AI: [添加属性: 时间依赖性]
    [检测到可能关联: 天文学设定]
    "我注意到你之前提到过天文体系,要将它们关联起来吗?"
用户: "好的"
AI: [创建关系: 星辰魔法 DERIVES_FROM 天文学设定]
    "那么掌握天文学知识的角色在施法上会有优势吗?"
...
```

