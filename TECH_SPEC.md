# OC设定工作台 - 技术规格文档 v0.2

## 核心架构决策

### 1. 数据存储架构

#### 1.1 Git版本控制
- **粒度**: Monorepo - 整个世界观一个Git仓库
- **目录结构**:
```
world_repo/
├── .git/
├── .worldconfig           # 世界观全局配置(时间体系定义等)
├── settings/              # 设定系目录
│   ├── _meta/             # 设定系索引和依赖关系
│   ├── magic_system/
│   │   ├── meta.json      # 设定系元数据
│   │   ├── README.md      # 设定系概述
│   │   ├── core/          # 核心设定
│   │   └── extensions/    # 扩展设定
│   ├── geography/
│   └── factions/
├── characters/            # 人物设定
│   ├── _index.json        # 人物索引
│   └── [character_id]/
│       ├── profile.md
│       ├── timeline.json
│       └── relations/
├── timelines/             # 时间线
│   └── main_timeline.json
└── floating_tips/         # 游离设定暂存
```

#### 1.2 数据库设计

**PostgreSQL Schema**:

```sql
-- 统一实体表
CREATE TABLE entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type VARCHAR(50) NOT NULL,  -- 'canonical_setting', 'character', 'timeline', 'event', 'item', 'floating_tip'
    title VARCHAR(500) NOT NULL,
    content TEXT,
    metadata JSONB DEFAULT '{}',
    
    -- Git相关
    git_path VARCHAR(1000),     -- 在Git仓库中的路径
    version_hash VARCHAR(64),   -- 当前版本的Git commit hash
    
    -- 状态管理
    status VARCHAR(20) DEFAULT 'active',  -- 'active', 'draft', 'archived', 'staged'
    
    -- 时间戳
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- 全文搜索
    search_vector tsvector,
    
    CONSTRAINT valid_entity_type CHECK (type IN (
        'canonical_setting', 'character', 'timeline', 'event', 
        'item', 'location', 'faction', 'floating_tip'
    ))
);

CREATE INDEX idx_entities_type ON entities(type);
CREATE INDEX idx_entities_status ON entities(status);
CREATE INDEX idx_entities_git_path ON entities(git_path);
CREATE INDEX idx_entities_search ON entities USING GIN(search_vector);

-- 关系表
CREATE TABLE relations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    target_id UUID NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    
    relation_type VARCHAR(50) NOT NULL,
    properties JSONB DEFAULT '{}',
    
    -- 关系强度/权重
    weight FLOAT DEFAULT 1.0,
    
    -- 时间有效性 (支持关系演变)
    valid_from BIGINT,  -- 自定义时间单位的数值
    valid_to BIGINT,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    CONSTRAINT valid_relation_type CHECK (relation_type IN (
        'DERIVES_FROM', 'BELONGS_TO', 'LINKED_TO', 'INFLUENCES',
        'CONFLICTS_WITH', 'EVOLVES_TO', 'KNOWS', 'LOCATED_AT',
        'OWNS', 'PARTICIPATES_IN', 'PARENT_OF', 'INHERITS_FROM'
    ))
);

CREATE INDEX idx_relations_source ON relations(source_id);
CREATE INDEX idx_relations_target ON relations(target_id);
CREATE INDEX idx_relations_type ON relations(relation_type);
CREATE INDEX idx_relations_time_range ON relations(valid_from, valid_to);

-- 时间体系定义表 (支持多时间体系)
CREATE TABLE time_systems (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL UNIQUE,
    description TEXT,
    
    -- 时间单位定义 (JSON数组)
    -- 例如: [{"unit": "era", "ratio": 1}, {"unit": "year", "ratio": 1000}, {"unit": "month", "ratio": 12}]
    units JSONB NOT NULL,
    
    -- 基准点: 用于和其他时间体系换算
    epoch_offset BIGINT DEFAULT 0,
    
    is_primary BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 时间线事件表
CREATE TABLE timeline_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_id UUID REFERENCES entities(id) ON DELETE CASCADE,
    
    title VARCHAR(500) NOT NULL,
    description TEXT,
    
    -- 时间戳 (使用主时间体系的数值)
    timestamp BIGINT NOT NULL,
    time_system_id UUID REFERENCES time_systems(id),
    
    -- 事件类型
    event_type VARCHAR(50),  -- 'birth', 'death', 'meeting', 'battle', 'discovery', etc.
    
    -- 参与者
    participants JSONB DEFAULT '[]',  -- [{"entity_id": "uuid", "role": "protagonist"}]
    
    -- 影响范围
    scope VARCHAR(50),  -- 'personal', 'local', 'regional', 'global'
    
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_timeline_events_timestamp ON timeline_events(timestamp);
CREATE INDEX idx_timeline_events_entity ON timeline_events(entity_id);

-- 暂存区 (Staging Area for AI Dialogue)
CREATE TABLE staging_canvas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,  -- 对话会话ID
    
    -- 画布数据
    canvas_data JSONB NOT NULL,  -- 包含所有元素: 文本、贴纸、图表、思维导图
    
    -- 衍生出的实体 (待确认)
    derived_entities JSONB DEFAULT '[]',
    derived_relations JSONB DEFAULT '[]',
    
    -- 状态
    status VARCHAR(20) DEFAULT 'active',  -- 'active', 'committed', 'discarded'
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- AI对话历史
CREATE TABLE ai_dialogues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    canvas_id UUID REFERENCES staging_canvas(id),
    
    role VARCHAR(20) NOT NULL,  -- 'user', 'assistant'
    content TEXT NOT NULL,
    
    -- 对话意图分析
    intent VARCHAR(50),  -- 'create', 'explore', 'challenge', 'deepen', 'link'
    
    -- 提取的结构化数据
    extracted_data JSONB,
    
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_ai_dialogues_session ON ai_dialogues(session_id);
CREATE INDEX idx_ai_dialogues_canvas ON ai_dialogues(canvas_id);
```

**Neo4j Graph Schema** (用于实时图谱查询):

```cypher
// 节点类型
(:Entity {
    id: UUID,
    type: String,
    title: String,
    status: String
})

// 关系类型 (带时间有效性)
-[:RELATION {
    type: String,
    weight: Float,
    valid_from: Int,
    valid_to: Int,
    properties: Map
}]->
```

### 2. 暂存区(Staging Canvas)设计

#### 2.1 画布元素类型

```typescript
// 前端数据模型
interface CanvasElement {
    id: string;
    type: 'text' | 'sticky' | 'mindmap' | 'chart' | 'latex' | 'drawing' | 'entity_draft';
    position: { x: number; y: number };
    size: { width: number; height: number };
    zIndex: number;
    data: any;  // 根据type不同而不同
}

interface TextElement extends CanvasElement {
    type: 'text';
    data: {
        content: string;  // Markdown
        fontSize: number;
        color: string;
    };
}

interface StickyElement extends CanvasElement {
    type: 'sticky';
    data: {
        content: string;
        color: string;
        tags: string[];  // 游离设定标签
        linkedEntities: string[];  // 关联的实体ID
    };
}

interface MindMapElement extends CanvasElement {
    type: 'mindmap';
    data: {
        root: MindMapNode;
    };
}

interface MindMapNode {
    id: string;
    label: string;
    children: MindMapNode[];
    linkedEntity?: string;  // 可选:链接到已有实体
}

interface EntityDraftElement extends CanvasElement {
    type: 'entity_draft';
    data: {
        entityType: string;
        title: string;
        properties: Record<string, any>;
        suggestedLinks: Array<{
            targetId: string;
            relationType: string;
            confidence: number;
        }>;
    };
}
```

#### 2.2 暂存区到知识图谱的提交流程

```python
# 伪代码
class StagingService:
    def commit_canvas(self, canvas_id: str):
        """将暂存区内容提交到知识图谱"""
        canvas = self.get_canvas(canvas_id)
        
        with transaction.atomic():
            # 1. 创建实体
            for element in canvas.derived_entities:
                entity = self.create_entity(element)
                
                # 2. 写入Git仓库
                git_path = self.generate_git_path(entity)
                self.write_to_git(entity, git_path)
                
                # 3. 提交到Git
                commit_hash = self.git_commit(
                    message=f"Add {entity.type}: {entity.title}",
                    files=[git_path]
                )
                
                # 4. 更新实体的version_hash
                entity.version_hash = commit_hash
                entity.save()
                
                # 5. 同步到Neo4j
                self.sync_to_neo4j(entity)
            
            # 6. 创建关系
            for relation in canvas.derived_relations:
                self.create_relation(relation)
                self.sync_relation_to_neo4j(relation)
            
            # 7. 标记画布为已提交
            canvas.status = 'committed'
            canvas.save()
```

### 3. AI产婆式对话引擎

#### 3.1 对话策略

```python
class SocraticDialogueEngine:
    """苏格拉底式产婆术对话引擎"""
    
    QUESTION_TYPES = {
        'fundamental': '基础性提问',      # 探索本质
        'challenging': '挑战性提问',      # 检验一致性
        'deepening': '深化性提问',        # 探索细节
        'connecting': '关联性提问',       # 建立联系
        'expanding': '扩展性提问',        # 发现空白
    }
    
    def analyze_user_input(self, message: str, context: DialogueContext):
        """分析用户输入,提取结构化信息"""
        # 使用LLM提取实体、属性、关系
        extraction = self.llm_extract(message, context)
        
        return {
            'entities': extraction.entities,
            'relations': extraction.relations,
            'inconsistencies': self.detect_inconsistencies(extraction, context),
            'gaps': self.find_knowledge_gaps(extraction, context),
        }
    
    def generate_next_question(self, analysis: dict, context: DialogueContext):
        """生成下一个问题"""
        # 优先级: 不一致性 > 基础性空白 > 深化 > 扩展
        
        if analysis['inconsistencies']:
            return self.challenge_question(analysis['inconsistencies'][0])
        
        if not context.has_foundation():
            return self.fundamental_question(analysis)
        
        if analysis['gaps']:
            return self.deepening_question(analysis['gaps'][0])
        
        return self.expanding_question(context)
    
    def challenge_question(self, inconsistency: Inconsistency):
        """生成挑战性问题"""
        prompt = f"""
        检测到不一致:
        - 新设定: {inconsistency.new_statement}
        - 已有设定: {inconsistency.existing_statement}
        
        生成一个苏格拉底式的挑战性问题,引导用户思考这个矛盾。
        """
        return self.llm_generate(prompt)
    
    def auto_suggest_links(self, new_entity: Entity, canvas: StagingCanvas):
        """自动建议关联"""
        # 使用向量相似度 + 图谱路径分析
        similar_entities = self.find_similar(new_entity)
        
        suggestions = []
        for entity in similar_entities:
            # 计算关联类型和置信度
            relation_type, confidence = self.infer_relation(new_entity, entity)
            if confidence > 0.6:
                suggestions.append({
                    'target': entity,
                    'relation_type': relation_type,
                    'confidence': confidence,
                    'reason': self.explain_suggestion(new_entity, entity)
                })
        
        return suggestions
```

#### 3.2 主动式对话触发器

```python
class ProactiveDialogueTrigger:
    """主动对话触发器"""
    
    def should_trigger(self, canvas: StagingCanvas) -> Optional[str]:
        """判断是否需要主动提问"""
        
        # 触发条件1: 检测到潜在矛盾
        conflicts = self.detect_conflicts(canvas)
        if conflicts:
            return self.generate_conflict_question(conflicts[0])
        
        # 触发条件2: 发现重要空白
        gaps = self.analyze_gaps(canvas)
        if gaps and gaps[0].importance > 0.8:
            return self.generate_gap_question(gaps[0])
        
        # 触发条件3: 实体过于孤立
        isolated = self.find_isolated_entities(canvas)
        if isolated:
            return self.generate_connection_question(isolated[0])
        
        # 触发条件4: 深化时机
        if self.should_deepen(canvas):
            return self.generate_deepening_question(canvas)
        
        return None
```

### 4. 多时间体系实现

```python
class TimeSystem:
    """时间体系管理"""
    
    def __init__(self, config: dict):
        self.name = config['name']
        self.units = config['units']  # [{'unit': 'era', 'ratio': 1}, ...]
        self.epoch_offset = config.get('epoch_offset', 0)
    
    def parse(self, time_str: str) -> int:
        """解析时间字符串到数值
        
        例如: "第三纪元12年" -> 3012
        """
        # 使用LLM + 正则表达式解析
        pass
    
    def format(self, timestamp: int) -> str:
        """格式化数值为可读字符串"""
        pass
    
    def convert_to(self, timestamp: int, target_system: 'TimeSystem') -> int:
        """转换到另一个时间体系"""
        # 通过epoch_offset进行换算
        pass

class TimelineService:
    """时间线服务"""
    
    def get_time_slice(self, timestamp: int, time_system: TimeSystem):
        """获取时间切片"""
        # 查询在这个时间点有效的所有关系
        relations = Relation.objects.filter(
            Q(valid_from__lte=timestamp) | Q(valid_from__isnull=True),
            Q(valid_to__gte=timestamp) | Q(valid_to__isnull=True)
        )
        
        # 查询在这个时间点存活的角色
        characters = self.get_alive_characters(timestamp)
        
        return {
            'timestamp': timestamp,
            'formatted_time': time_system.format(timestamp),
            'characters': characters,
            'relations': relations,
            'events': self.get_events_at(timestamp)
        }
    
    def get_relation_evolution(self, char1_id, char2_id, time_range):
        """获取关系演变历史"""
        relations = Relation.objects.filter(
            source_id=char1_id,
            target_id=char2_id,
            valid_from__range=time_range
        ).order_by('valid_from')
        
        return relations
```

### 5. 双向索引与图谱查询

```python
class GraphQueryService:
    """图谱查询服务"""
    
    def get_bidirectional_links(self, entity_id: str):
        """获取双向链接"""
        query = """
        MATCH (e:Entity {id: $entity_id})-[r]-(connected)
        RETURN e, r, connected
        """
        return self.neo4j_execute(query, entity_id=entity_id)
    
    def find_influence_path(self, source_id: str, target_id: str, max_depth=5):
        """查找影响路径"""
        query = """
        MATCH path = shortestPath(
            (source:Entity {id: $source_id})-[*..${max_depth}]->(target:Entity {id: $target_id})
        )
        RETURN path
        """
        return self.neo4j_execute(query, source_id=source_id, target_id=target_id)
    
    def detect_conflicts(self, entity_id: str):
        """检测设定冲突"""
        # 查找所有CONFLICTS_WITH关系
        query = """
        MATCH (e:Entity {id: $entity_id})-[r:CONFLICTS_WITH]-(conflicting)
        RETURN conflicting, r.properties as reason
        """
        return self.neo4j_execute(query, entity_id=entity_id)
```

## 下一步: 技术栈细化

接下来需要确认:
1. 前端图谱可视化库选择 (D3.js vs Cytoscape.js vs 自定义)
2. 暂存区画布实现 (Fabric.js vs Konva.js vs TLDraw)
3. AI模型选择 (GPT-4 vs Claude vs 本地部署)
4. 部署方案 (Docker Compose vs Kubernetes)

