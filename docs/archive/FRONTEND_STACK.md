> 历史文档：保留早期设计与规划，未实施条目、版本和示例不代表当前能力。当前入口见 [项目 README](../../README.md)、[架构说明](../architecture.md) 和 [开发指南](../development.md)。

# 前端技术栈详细规格

## 核心技术栈

### 框架与构建
- **Vue 3.4+** (Composition API)
- **TypeScript 5.x**
- **Vite 5.x** (构建工具)
- **Pinia** (状态管理)
- **Vue Router 4** (路由)

### UI组件库
- **Element Plus** (基础UI组件)
- **Tailwind CSS** (样式工具)

### 画布与可视化
- **TLDraw** - 无限画布/白板
- **Cytoscape.js** - 知识图谱可视化
- **TipTap** - Markdown编辑器
- **KaTeX** - LaTeX数学公式渲染
- **Mermaid** - 图表和思维导图
- **Chart.js** - 数据可视化

### 其他库
- **Axios** - HTTP客户端
- **Day.js** - 时间处理
- **Lodash-es** - 工具函数
- **VueUse** - Vue组合式函数集合

## 前端目录结构

```
frontend/
├── public/
│   └── favicon.ico
├── src/
│   ├── main.ts                 # 应用入口
│   ├── App.vue
│   ├── router/
│   │   └── index.ts            # 路由配置
│   ├── stores/                 # Pinia状态管理
│   │   ├── graph.ts            # 图谱状态
│   │   ├── canvas.ts           # 画布状态
│   │   ├── timeline.ts         # 时间线状态
│   │   ├── dialogue.ts         # AI对话状态
│   │   └── worldview.ts        # 世界观全局状态
│   ├── views/                  # 页面视图
│   │   ├── Dashboard.vue       # 工作台总览
│   │   ├── GraphView.vue       # 知识图谱视图
│   │   ├── CanvasView.vue      # 暂存区画布视图
│   │   ├── TimelineView.vue    # 时间线视图
│   │   ├── EntityEditor.vue    # 实体编辑器
│   │   └── DialogueView.vue    # AI对话视图
│   ├── components/
│   │   ├── canvas/             # 画布相关组件
│   │   │   ├── TLDrawCanvas.vue
│   │   │   ├── StickyNote.vue
│   │   │   ├── MindMap.vue
│   │   │   └── EntityDraft.vue
│   │   ├── graph/              # 图谱相关组件
│   │   │   ├── CytoscapeGraph.vue
│   │   │   ├── GraphFilters.vue
│   │   │   ├── NodeDetail.vue
│   │   │   └── RelationEditor.vue
│   │   ├── editor/             # 编辑器相关
│   │   │   ├── TipTapEditor.vue
│   │   │   ├── WikiLinkPlugin.vue
│   │   │   ├── LatexPlugin.vue
│   │   │   └── VersionHistory.vue
│   │   ├── timeline/           # 时间线相关
│   │   │   ├── TimelineSlider.vue
│   │   │   ├── EventCard.vue
│   │   │   ├── TimeSliceGraph.vue
│   │   │   └── CharacterLifeline.vue
│   │   ├── dialogue/           # AI对话相关
│   │   │   ├── ChatInterface.vue
│   │   │   ├── MessageBubble.vue
│   │   │   ├── SuggestionPanel.vue
│   │   │   └── ConflictAlert.vue
│   │   └── common/             # 通用组件
│   │       ├── SearchBar.vue
│   │       ├── TagCloud.vue
│   │       └── LoadingState.vue
│   ├── composables/            # 组合式函数
│   │   ├── useGraph.ts
│   │   ├── useCanvas.ts
│   │   ├── useTimeline.ts
│   │   ├── useDialogue.ts
│   │   └── useWebSocket.ts
│   ├── services/               # API服务
│   │   ├── api.ts              # API基础配置
│   │   ├── entity.service.ts
│   │   ├── relation.service.ts
│   │   ├── timeline.service.ts
│   │   ├── dialogue.service.ts
│   │   └── graph.service.ts
│   ├── types/                  # TypeScript类型定义
│   │   ├── entity.ts
│   │   ├── relation.ts
│   │   ├── canvas.ts
│   │   ├── timeline.ts
│   │   └── dialogue.ts
│   ├── utils/                  # 工具函数
│   │   ├── graph-layout.ts
│   │   ├── time-parser.ts
│   │   └── markdown.ts
│   └── assets/
│       ├── styles/
│       │   ├── main.css
│       │   └── tailwind.css
│       └── icons/
├── index.html
├── package.json
├── tsconfig.json
├── vite.config.ts
├── tailwind.config.js
└── README.md
```

## 核心组件设计

### 1. 暂存区画布 (TLDraw Canvas)

```vue
<!-- components/canvas/TLDrawCanvas.vue -->
<script setup lang="ts">
import { Tldraw, TLEditorComponents } from '@tldraw/tldraw'
import '@tldraw/tldraw/tldraw.css'
import { useCanvasStore } from '@/stores/canvas'
import { ref, watch } from 'vue'

const canvasStore = useCanvasStore()

// 自定义工具
const components: TLEditorComponents = {
  // 添加游离设定贴纸工具
  // 添加实体草稿工具
  // 添加思维导图工具
}

// 监听画布变化,实时保存
watch(() => canvasStore.currentCanvas, (newCanvas) => {
  // 保存到后端
}, { deep: true })
</script>

<template>
  <div class="canvas-container h-screen">
    <Tldraw 
      :components="components"
      @mount="onMount"
      @change="onChange"
    />
  </div>
</template>
```

### 2. 知识图谱 (Cytoscape.js)

```vue
<!-- components/graph/CytoscapeGraph.vue -->
<script setup lang="ts">
import cytoscape from 'cytoscape'
import { onMounted, ref, watch } from 'vue'
import { useGraphStore } from '@/stores/graph'

const graphStore = useGraphStore()
const container = ref<HTMLElement>()
let cy: cytoscape.Core

const initGraph = () => {
  cy = cytoscape({
    container: container.value,
    elements: graphStore.elements,
    style: [
      {
        selector: 'node',
        style: {
          'background-color': '#666',
          'label': 'data(title)',
          'text-valign': 'center',
          'text-halign': 'center',
          'width': 'label',
          'height': 'label',
          'padding': '10px'
        }
      },
      {
        selector: 'edge',
        style: {
          'width': 2,
          'line-color': '#ccc',
          'target-arrow-color': '#ccc',
          'target-arrow-shape': 'triangle',
          'label': 'data(relationType)',
          'curve-style': 'bezier'
        }
      },
      // 不同实体类型的样式
      {
        selector: 'node[type="character"]',
        style: { 'background-color': '#3498db' }
      },
      {
        selector: 'node[type="canonical_setting"]',
        style: { 'background-color': '#e74c3c' }
      },
      {
        selector: 'node[type="floating_tip"]',
        style: { 
          'background-color': '#f39c12',
          'shape': 'diamond'
        }
      }
    ],
    layout: {
      name: 'cose',
      animate: true,
      nodeRepulsion: 8000
    }
  })

  // 节点点击事件
  cy.on('tap', 'node', (evt) => {
    const node = evt.target
    graphStore.selectNode(node.id())
  })

  // 双向链接高亮
  cy.on('mouseover', 'node', (evt) => {
    const node = evt.target
    const connectedEdges = node.connectedEdges()
    connectedEdges.addClass('highlighted')
  })
}

onMounted(() => {
  initGraph()
})

watch(() => graphStore.elements, () => {
  cy.json({ elements: graphStore.elements })
  cy.layout({ name: 'cose' }).run()
})
</script>

<template>
  <div ref="container" class="graph-container w-full h-full"></div>
</template>
```

### 3. TipTap编辑器 (支持WikiLink)

```vue
<!-- components/editor/TipTapEditor.vue -->
<script setup lang="ts">
import { useEditor, EditorContent } from '@tiptap/vue-3'
import StarterKit from '@tiptap/starter-kit'
import { Markdown } from 'tiptap-markdown'
import { Mathematics } from '@tiptap-pro/extension-mathematics'
import { WikiLink } from '@/extensions/wikilink'
import 'katex/dist/katex.min.css'

const props = defineProps<{
  modelValue: string
  entityType: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string): void
}>()

const editor = useEditor({
  extensions: [
    StarterKit,
    Markdown,
    Mathematics,
    WikiLink.configure({
      onLinkClick: (linkText) => {
          // 导航到链接的实体
        },
        getSuggestions: async (query) => {
          // 从后端获取实体建议
          return []
        }
      })
  ],
  content: props.modelValue,
  onUpdate: ({ editor }) => {
    emit('update:modelValue', editor.storage.markdown.getMarkdown())
  }
})
</script>

<template>
  <div class="editor-container">
    <EditorContent :editor="editor" />
  </div>
</template>
```

### 4. AI对话界面

```vue
<!-- components/dialogue/ChatInterface.vue -->
<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { useDialogueStore } from '@/stores/dialogue'
import MessageBubble from './MessageBubble.vue'
import SuggestionPanel from './SuggestionPanel.vue'

const dialogueStore = useDialogueStore()
const userInput = ref('')
const messagesContainer = ref<HTMLElement>()

const sendMessage = async () => {
  if (!userInput.value.trim()) return
  
  await dialogueStore.sendMessage(userInput.value)
  userInput.value = ''
  
  // 滚动到底部
  nextTick(() => {
    messagesContainer.value?.scrollTo({
      top: messagesContainer.value.scrollHeight,
      behavior: 'smooth'
    })
  })
}

// 接受AI建议
const acceptSuggestion = (suggestion: any) => {
  dialogueStore.acceptEntitySuggestion(suggestion)
}
</script>

<template>
  <div class="chat-interface flex flex-col h-full">
    <!-- 消息区域 -->
    <div ref="messagesContainer" class="messages flex-1 overflow-y-auto p-4">
      <MessageBubble
        v-for="msg in dialogueStore.messages"
        :key="msg.id"
        :message="msg"
      />
    </div>

    <!-- 建议面板 -->
    <SuggestionPanel
      v-if="dialogueStore.currentSuggestions.length"
      :suggestions="dialogueStore.currentSuggestions"
      @accept="acceptSuggestion"
    />

    <!-- 输入区域 -->
    <div class="input-area p-4 border-t">
      <div class="flex gap-2">
        <input
          v-model="userInput"
          type="text"
          class="flex-1 px-4 py-2 border rounded-lg"
          placeholder="和AI对话,共同构建世界观..."
          @keyup.enter="sendMessage"
        />
        <button
          @click="sendMessage"
          class="px-6 py-2 bg-blue-500 text-white rounded-lg hover:bg-blue-600"
        >
          发送
        </button>
      </div>
    </div>
  </div>
</template>
```

### 5. 时间切片器

```vue
<!-- components/timeline/TimeSliceGraph.vue -->
<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useTimelineStore } from '@/stores/timeline'
import CytoscapeGraph from '@/components/graph/CytoscapeGraph.vue'

const timelineStore = useTimelineStore()
const currentTime = ref(0)

// 获取当前时间切片的关系图数据
const sliceData = computed(() => {
  return timelineStore.getTimeSlice(currentTime.value)
})

// 格式化时间显示
const formattedTime = computed(() => {
  return timelineStore.formatTime(currentTime.value)
})
</script>

<template>
  <div class="time-slice-graph">
    <!-- 时间滑块 -->
    <div class="time-slider p-4">
      <div class="text-center text-2xl font-bold mb-4">
        {{ formattedTime }}
      </div>
      <input
        v-model="currentTime"
        type="range"
        :min="timelineStore.minTime"
        :max="timelineStore.maxTime"
        :step="timelineStore.timeStep"
        class="w-full"
      />
      <div class="flex justify-between text-sm text-gray-500 mt-2">
        <span>{{ timelineStore.formatTime(timelineStore.minTime) }}</span>
        <span>{{ timelineStore.formatTime(timelineStore.maxTime) }}</span>
      </div>
    </div>

    <!-- 关系图 -->
    <div class="graph-area flex-1">
      <CytoscapeGraph :elements="sliceData" />
    </div>

    <!-- 事件列表 -->
    <div class="events-panel w-80 border-l p-4 overflow-y-auto">
      <h3 class="font-bold mb-4">此时发生的事件</h3>
      <div
        v-for="event in timelineStore.getEventsAt(currentTime)"
        :key="event.id"
        class="event-card mb-3 p-3 border rounded"
      >
        <div class="font-semibold">{{ event.title }}</div>
        <div class="text-sm text-gray-600">{{ event.description }}</div>
      </div>
    </div>
  </div>
</template>
```

## TypeScript 类型定义

```typescript
// types/entity.ts
export interface Entity {
  id: string
  type: EntityType
  title: string
  content: string
  metadata: Record<string, any>
  gitPath?: string
  versionHash?: string
  status: 'active' | 'draft' | 'archived' | 'staged'
  createdAt: string
  updatedAt: string
}

export type EntityType =
  | 'canonical_setting'
  | 'character'
  | 'timeline'
  | 'event'
  | 'item'
  | 'location'
  | 'faction'
  | 'floating_tip'

// types/relation.ts
export interface Relation {
  id: string
  sourceId: string
  targetId: string
  relationType: RelationType
  properties: Record<string, any>
  weight: number
  validFrom?: number
  validTo?: number
  createdAt: string
}

export type RelationType =
  | 'DERIVES_FROM'
  | 'BELONGS_TO'
  | 'LINKED_TO'
  | 'INFLUENCES'
  | 'CONFLICTS_WITH'
  | 'EVOLVES_TO'
  | 'KNOWS'
  | 'LOCATED_AT'
  | 'OWNS'
  | 'PARTICIPATES_IN'

// types/canvas.ts
export interface CanvasElement {
  id: string
  type: 'text' | 'sticky' | 'mindmap' | 'chart' | 'latex' | 'drawing' | 'entity_draft'
  position: { x: number; y: number }
  size: { width: number; height: number }
  zIndex: number
  data: any
}

export interface StagingCanvas {
  id: string
  sessionId: string
  canvasData: CanvasElement[]
  derivedEntities: Entity[]
  derivedRelations: Relation[]
  status: 'active' | 'committed' | 'discarded'
  createdAt: string
  updatedAt: string
}

// types/timeline.ts
export interface TimelineEvent {
  id: string
  entityId?: string
  title: string
  description: string
  timestamp: number
  timeSystemId: string
  eventType: string
  participants: Array<{
    entityId: string
    role: string
  }>
  scope: 'personal' | 'local' | 'regional' | 'global'
  createdAt: string
}

export interface TimeSystem {
  id: string
  name: string
  description: string
  units: Array<{
    unit: string
    ratio: number
  }>
  epochOffset: number
  isPrimary: boolean
}

// types/dialogue.ts
export interface DialogueMessage {
  id: string
  sessionId: string
  role: 'user' | 'assistant'
  content: string
  intent?: 'create' | 'explore' | 'challenge' | 'deepen' | 'link'
  extractedData?: any
  timestamp: string
}

export interface EntitySuggestion {
  targetId: string
  relationType: RelationType
  confidence: number
  reason: string
}
```

## Pinia状态管理示例

```typescript
// stores/graph.ts
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Entity, Relation } from '@/types'
import { graphService } from '@/services/graph.service'

export const useGraphStore = defineStore('graph', () => {
  const entities = ref<Entity[]>([])
  const relations = ref<Relation[]>([])
  const selectedNodeId = ref<string | null>(null)

  // Cytoscape elements格式
  const elements = computed(() => {
    const nodes = entities.value.map(entity => ({
      data: {
        id: entity.id,
        title: entity.title,
        type: entity.type,
        ...entity.metadata
      }
    }))

    const edges = relations.value.map(relation => ({
      data: {
        id: relation.id,
        source: relation.sourceId,
        target: relation.targetId,
        relationType: relation.relationType,
        weight: relation.weight
      }
    }))

    return { nodes, edges }
  })

  const selectedNode = computed(() =>
    entities.value.find(e => e.id === selectedNodeId.value)
  )

  const loadGraph = async () => {
    const data = await graphService.getFullGraph()
    entities.value = data.entities
    relations.value = data.relations
  }

  const selectNode = (nodeId: string) => {
    selectedNodeId.value = nodeId
  }

  const getBidirectionalLinks = async (entityId: string) => {
    return await graphService.getBidirectionalLinks(entityId)
  }

  return {
    entities,
    relations,
    elements,
    selectedNode,
    loadGraph,
    selectNode,
    getBidirectionalLinks
  }
})
```

## WebSocket实时同步

```typescript
// composables/useWebSocket.ts
import { ref, onMounted, onUnmounted } from 'vue'
import { useGraphStore } from '@/stores/graph'
import { useCanvasStore } from '@/stores/canvas'

export function useWebSocket() {
  const ws = ref<WebSocket | null>(null)
  const isConnected = ref(false)

  const connect = () => {
    ws.value = new WebSocket('ws://localhost:8000/ws')

    ws.value.onopen = () => {
      isConnected.value = true
      console.log('WebSocket connected')
    }

    ws.value.onmessage = (event) => {
      const data = JSON.parse(event.data)
      handleMessage(data)
    }

    ws.value.onclose = () => {
      isConnected.value = false
      // 重连逻辑
      setTimeout(connect, 3000)
    }
  }

  const handleMessage = (data: any) => {
    const graphStore = useGraphStore()
    const canvasStore = useCanvasStore()

    switch (data.type) {
      case 'entity_created':
        graphStore.entities.push(data.entity)
        break
      case 'relation_created':
        graphStore.relations.push(data.relation)
        break
      case 'canvas_updated':
        if (data.sessionId === canvasStore.currentSessionId) {
          canvasStore.syncCanvas(data.canvas)
        }
        break
      case 'ai_message':
        // 处理AI主动提问
        break
    }
  }

  onMounted(connect)
  onUnmounted(() => ws.value?.close())

  return { isConnected }
}
```

## 下一步

1. 创建后端Django项目结构
2. 实现核心API端点
3. 配置PostgreSQL和Neo4j
4. 实现GitPython集成
5. 集成AI对话引擎

