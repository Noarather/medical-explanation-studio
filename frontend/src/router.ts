import { createRouter, createWebHashHistory } from "vue-router"
import DashboardPage from "./pages/DashboardPage.vue"
import ImportPage from "./pages/ImportPage.vue"
import QuestionsPage from "./pages/QuestionsPage.vue"
import ReviewPage from "./pages/ReviewPage.vue"
import LibraryPage from "./pages/LibraryPage.vue"
import TagsPage from "./pages/TagsPage.vue"
import ExportsPage from "./pages/ExportsPage.vue"

export const NAV_ITEMS = [
  { path: "/", name: "dashboard", title: "工作台", icon: "House" },
  { path: "/import", name: "import", title: "题目导入", icon: "UploadSimple" },
  { path: "/questions", name: "questions", title: "题目管理", icon: "Files" },
  { path: "/review", name: "review", title: "审核工作台", icon: "CheckSquare" },
  { path: "/library", name: "library", title: "教材库", icon: "Books" },
  { path: "/tags", name: "tags", title: "标签管理", icon: "Tag" },
  { path: "/export", name: "export", title: "导出中心", icon: "Export" },
] as const

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: DashboardPage },
    { path: "/import", component: ImportPage },
    { path: "/questions", component: QuestionsPage },
    { path: "/review", component: ReviewPage },
    { path: "/library", component: LibraryPage },
    { path: "/tags", component: TagsPage },
    { path: "/export", component: ExportsPage },
  ],
})
