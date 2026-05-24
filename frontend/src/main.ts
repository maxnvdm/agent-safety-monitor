import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import SessionList from './views/SessionList.vue'
import SessionDetail from './views/SessionDetail.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: SessionList },
    { path: '/sessions/:id', component: SessionDetail, props: true },
  ],
})

createApp(App).use(router).mount('#app')
