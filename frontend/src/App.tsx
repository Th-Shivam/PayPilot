import { useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { IntroScreen } from './components/IntroScreen'
import { SignUpPage } from './pages/SignUpPage'
import { SignInPage } from './pages/SignInPage'
import './App.css'

const INTRO_DURATION_MS = 4000

export function App() {
  const [showIntro, setShowIntro] = useState(true)

  useEffect(() => {
    const timer = window.setTimeout(() => setShowIntro(false), INTRO_DURATION_MS)
    return () => window.clearTimeout(timer)
  }, [])

  if (showIntro) {
    return <IntroScreen />
  }

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/sign-up" element={<SignUpPage />} />
        <Route path="/sign-in" element={<SignInPage />} />
        <Route path="/" element={<Navigate to="/sign-up" replace />} />
        <Route path="*" element={<Navigate to="/sign-up" replace />} />
      </Routes>
    </BrowserRouter>
  )
}

export default App
