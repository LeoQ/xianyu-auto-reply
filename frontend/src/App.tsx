import React from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { MainLayout } from '@/components/layout/MainLayout'
import { Login } from '@/pages/auth/Login'
import { Register } from '@/pages/auth/Register'
import { Dashboard } from '@/pages/dashboard/Dashboard'
import { Accounts } from '@/pages/accounts/Accounts'
import { Items } from '@/pages/items/Items'
import { Orders } from '@/pages/orders/Orders'
import { Keywords } from '@/pages/keywords/Keywords'
import { Cards } from '@/pages/cards/Cards'
import { Delivery } from '@/pages/delivery/Delivery'
import { NotificationChannels } from '@/pages/notifications/NotificationChannels'
import { MessageNotifications } from '@/pages/notifications/MessageNotifications'
import { Settings } from '@/pages/settings/Settings'
import { ItemReplies } from '@/pages/item-replies/ItemReplies'
import { ItemSearch } from '@/pages/search/ItemSearch'
import { Users } from '@/pages/admin/Users'
import { Logs } from '@/pages/admin/Logs'
import { RiskLogs } from '@/pages/admin/RiskLogs'
import { DataManagement } from '@/pages/admin/DataManagement'
import { Toast } from '@/components/common/Toast'

// Protected route wrapper
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  return <>{children}</>
}

function App() {
  return (
    <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      {/* 全局 Toast 组件 */}
      <Toast />
      <Routes>
        {/* Public routes */}
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />

        {/* Protected routes */}
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <MainLayout />
            </ProtectedRoute>
          }
        >
          <Route index element={<Navigate to="/dashboard" replace />} />
          <Route path="dashboard" element={<Dashboard />} />
          <Route path="accounts" element={<Accounts />} />
          <Route path="items" element={<Items />} />
          <Route path="orders" element={<Orders />} />
          <Route path="keywords" element={<Keywords />} />
          <Route path="item-replies" element={<ItemReplies />} />
          <Route path="cards" element={<Cards />} />
          <Route path="delivery" element={<Delivery />} />
          <Route path="notification-channels" element={<NotificationChannels />} />
          <Route path="message-notifications" element={<MessageNotifications />} />
          <Route path="item-search" element={<ItemSearch />} />
          <Route path="settings" element={<Settings />} />

          {/* Admin routes */}
          <Route path="admin/users" element={<Users />} />
          <Route path="admin/logs" element={<Logs />} />
          <Route path="admin/risk-logs" element={<RiskLogs />} />
          <Route path="admin/data" element={<DataManagement />} />
        </Route>

        {/* Catch all */}
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
