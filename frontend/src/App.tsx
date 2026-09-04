import { useEffect, useState, type ReactElement } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { IntroScreen } from './components/IntroScreen'
import { WelcomeScreen } from './components/WelcomeScreen'
import { DashboardPage } from './pages/DashboardPage'
import './App.css'

const INTRO_DURATION_MS = 4000

export function App(): ReactElement {
  const [showIntro, setShowIntro] = useState(true)

  useEffect(() => {
    const timer = window.setTimeout(() => setShowIntro(false), INTRO_DURATION_MS)
    return () => window.clearTimeout(timer)
  }, [])

  if (showIntro) return <IntroScreen />

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<WelcomeScreen />} />
        <Route path="/sign-in" element={<WelcomeScreen />} />
        <Route path="/sign-up" element={<WelcomeScreen />} />
        <Route path="/dashboard" element={<DashboardPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
