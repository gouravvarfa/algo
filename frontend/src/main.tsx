import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { App } from './app/App'
import { SessionProvider } from './lib/session'
import { ToastProvider } from './lib/toast'
import { applyMotion } from './lib/theme'
import './styles/app.css'
import './styles/mobile.css'      // phone / small-tablet layout: hamburger menu and drawer
import './styles/aurora.css'     // optional animated background: delete this line (and the file) to go back
import './styles/perf.css'       // smooth scrolling: no live blur on cards, transform-only background motion (load last)

const queryClient = new QueryClient({
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1, staleTime: 5_000 } },
})

applyMotion()
createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter basename="/app">
        <SessionProvider>
          <ToastProvider>
            <App />
          </ToastProvider>
        </SessionProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)
