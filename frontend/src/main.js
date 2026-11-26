import { createApp } from 'vue'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import DriverView from './views/DriverView.vue'
import WaitlistView from './views/WaitlistView.vue'
import OperationsView from './views/OperationsView.vue'
import './style.css'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/driver' },
    { path: '/driver', component: DriverView, name: 'driver' },
    { path: '/waitlist', component: WaitlistView, name: 'waitlist' },
    { path: '/operations', component: OperationsView, name: 'operations' },
  ],
})

createApp(App).use(router).mount('#app')
