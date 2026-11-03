import { useCallback, useSyncExternalStore } from "react";
import { authApi } from "./authApi";
import type { Profile } from "./authTypes";
import { tokenStore } from "./registryApi";
import { useApi, type ApiState } from "./useApi";
import { useToken } from "./useRegistry";

/** Server-side session tokens start with this prefix; static service tokens do not and have no sign-in profile. */
export const SESSION_PREFIX = "atl_s_";

export function useSessionExpired(): boolean {
  return useSyncExternalStore(tokenStore.subscribe, tokenStore.expired, () => false);
}

/** Profile of the signed-in user, or undefined when nobody is signed in with a session. Never throws into the UI. */
export function useProfile(): ApiState<Profile> & { signedIn: boolean } {
  const token = useToken();
  const signedIn = token.startsWith(SESSION_PREFIX);
  const fetcher = useCallback(
    (signal: AbortSignal) => (signedIn ? authApi.me(signal) : Promise.reject(new Error("not signed in"))),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [signedIn, token],
  );
  const state = useApi(fetcher);
  return { ...state, signedIn, data: signedIn ? state.data : undefined };
}

export const can = (profile: Profile | undefined, permission: string): boolean =>
  !!profile && profile.permissions.includes(permission);
