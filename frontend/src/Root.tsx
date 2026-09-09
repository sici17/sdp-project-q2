import { useCallback, useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'

import App from './App'
import { FleetView } from './components/FleetView'
import { SignInScreen } from './components/SignInScreen'
import { getAuthSession, logoutAuthSession, startProviderLogout } from './services/api'
import type { AuthSession } from './types/contracts'

type Route =
  | { name: 'signin' }
  | { name: 'fleet' }
  | { name: 'machine'; machineId: string }

/**
 * Read the route from the address bar.
 *
 * `/machines/:idOrSerial` is the QR target: a code on a machine may encode
 * either identifier, and an operator reads the serial off the machine plate.
 * `/m/:machineId` is kept so codes printed against the earlier scheme still work.
 *
 * An unrecognised path resolves to the fleet, never to a default machine.
 * Silently substituting one machine for another would show an operator the
 * wrong manual for the machine in front of them.
 */
function readRoute(): Route {
  const parts = window.location.pathname.split('/').filter(Boolean)
  const params = new URLSearchParams(window.location.search)

  if (parts[0] === 'machines' && parts[1]) {
    return { name: 'machine', machineId: decodeURIComponent(parts[1]) }
  }
  if (parts[0] === 'm' && parts[1]) {
    return { name: 'machine', machineId: decodeURIComponent(parts[1]) }
  }
  if (parts[0] === 'login') {
    return { name: 'signin' }
  }
  const legacy = params.get('machineId')
  if (legacy) {
    return { name: 'machine', machineId: legacy }
  }
  return { name: 'fleet' }
}

function navigate(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/**
 * Chooses between signing in, the fleet, and one machine's workspace.
 *
 * The machine workspace is the existing operator console, which stays the
 * primary surface: manual first, chat beside it.
 */
export function Root() {
  const [route, setRoute] = useState<Route>(readRoute)
  const [session, setSession] = useState<AuthSession | null>(null)
  const [checked, setChecked] = useState(false)
  /** Where an unauthenticated visitor was heading, replayed after sign-in. */
  const [pendingRoute, setPendingRoute] = useState<Route | null>(() => {
    const initial = readRoute()
    return initial.name === 'signin' ? null : initial
  })

  useEffect(() => {
    const onPopState = () => setRoute(readRoute())
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  const refreshSession = useCallback(async () => {
    try {
      const next = await getAuthSession()
      setSession(next)
      return next
    } catch {
      setSession(null)
      return null
    } finally {
      setChecked(true)
    }
  }, [])

  useEffect(() => {
    // A browser arriving with no session is asked who it is. Bootstrapping one
    // silently used to sign every visitor in as the gateway's default demo
    // identity, which happens to be the most privileged user in the dataset -
    // so the access model the whole platform is built around was never the
    // thing being demonstrated. A QR scan still works: the scanned machine is
    // remembered below and reopened once someone has said who they are.
    void (async () => {
      await refreshSession()
    })()
  }, [refreshSession])

  const handleSignedIn = useCallback(async () => {
    const current = await refreshSession()
    if (!(current?.authenticated || current?.mode === 'off')) {
      return
    }
    // Back to whatever was asked for, so scanning a code and signing in lands
    // on that machine rather than dropping the operator at the fleet list.
    const resumed = pendingRoute ?? { name: 'fleet' as const }
    setPendingRoute(null)
    navigate(resumed.name === 'machine' ? `/machines/${encodeURIComponent(resumed.machineId)}` : '/')
    setRoute(resumed)
  }, [pendingRoute, refreshSession])

  const handleSignOut = useCallback(async () => {
    window.sessionStorage.clear()
    // Dropped deliberately: the next person to sign in may not own the machine
    // this one was looking at.
    setPendingRoute(null)
    try {
      // The session cookie has to go server-side too. Clearing local state and
      // showing the sign-in screen while the cookie stays valid is a sign-out
      // in appearance only, and on a shared plant-floor tablet that matters.
      const logout = await logoutAuthSession()
      if (logout.logoutRequired) {
        startProviderLogout()
        return
      }
    } catch {
      // The screen below still refuses to render anything without a session.
    }
    setSession(null)
    navigate('/login')
    setRoute({ name: 'signin' })
  }, [])

  if (!checked) {
    return (
      <Box sx={{ display: 'flex', justifyContent: 'center', p: 6 }}>
        <CircularProgress />
      </Box>
    )
  }

  // No identity, no data. Every screen below is scoped to a company and a
  // visibility level, so there is nothing to render before one is chosen.
  //
  // `GATEWAY_AUTH_MODE=off` is the exception, and the gateway reports it as
  // `authenticated: false` because there genuinely is no identity - the whole
  // access model is switched off. Asking that deployment to sign in would be a
  // dead end: there is nothing to sign in to.
  if (route.name === 'signin' || !(session?.authenticated || session?.mode === 'off')) {
    return <SignInScreen onSignedIn={() => void handleSignedIn()} />
  }

  if (route.name === 'machine') {
    return <App machineId={route.machineId} session={session} onBackToFleet={() => navigate('/')} />
  }

  return (
    <FleetView
      session={session}
      onOpenMachine={(machineId) => {
        navigate(`/machines/${encodeURIComponent(machineId)}`)
        setRoute({ name: 'machine', machineId })
      }}
      onSignOut={() => void handleSignOut()}
    />
  )
}
