import { useEffect, useState } from 'react'

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '')

export default function App() {
  const [apiStatus, setApiStatus] = useState('checking')

  useEffect(() => {
    const controller = new AbortController()

    async function checkApi() {
      try {
        const response = await fetch(`${apiBaseUrl}/api/health`, {
          signal: controller.signal,
          cache: 'no-store',
        })
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const body = await response.json()
        if (body.status !== 'ok') throw new Error('Unexpected health response')
        setApiStatus('online')
      } catch {
        if (!controller.signal.aborted) setApiStatus('offline')
      }
    }

    checkApi()
    return () => controller.abort()
  }, [])

  const labels = {
    checking: '백엔드 확인 중',
    online: '백엔드 연결 정상',
    offline: '백엔드 연결 실패',
  }

  return (
    <main className="shell">
      <header className="header">
        <span className="brand-mark">YF</span>
        <span className="brand-name">청년 금융 Multi-Agent</span>
        <span className="environment">Starter · v0.1</span>
      </header>

      <section className="hero">
        <p className="eyebrow">PROJECT FOUNDATION</p>
        <h1>금융 상담 서비스를 위한<br />기본 개발 환경</h1>
        <p className="lead">React 프런트엔드와 FastAPI 백엔드 연결을 확인하는 첫 화면입니다. 실제 상담 기능이나 금융정보 처리는 아직 연결되지 않았습니다.</p>
        <div className={`status ${apiStatus}`} role="status" aria-live="polite">
          <span className="status-dot" aria-hidden="true" />
          {labels[apiStatus]}
        </div>
      </section>

      <section className="grid" aria-label="구성 요소">
        <article className="panel">
          <div className="panel-number">01</div>
          <h2>Frontend</h2>
          <p>React + Vite</p>
          <span className="detail">Vercel 배포 설정 포함</span>
        </article>
        <article className="panel">
          <div className="panel-number">02</div>
          <h2>Backend</h2>
          <p>FastAPI</p>
          <span className="detail">헬스체크 + Dockerfile</span>
        </article>
        <article className="panel">
          <div className="panel-number">03</div>
          <h2>Agent</h2>
          <p>LangGraph 골격</p>
          <span className="detail">Supervisor / node_agents</span>
        </article>
      </section>

      <p className="footnote">다음 단계: 인증 및 데이터 접근 경계 설계 → Agent 구현 → 근거·지식그래프 화면 연결</p>
    </main>
  )
}
