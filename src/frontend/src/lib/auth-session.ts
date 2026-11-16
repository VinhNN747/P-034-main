type AuthenticationClaims = { amr?: unknown; is_anonymous?: unknown };

export function isRecoverySession(claims: AuthenticationClaims) {
  return Array.isArray(claims.amr) && claims.amr.some(
    (method) => method && typeof method === "object" && method.method === "recovery",
  );
}

export function isAuthenticationComplete(claims: AuthenticationClaims) {
  return claims.is_anonymous !== true && !isRecoverySession(claims);
}

export function hasRecentRecoveryConfirmation(claims: AuthenticationClaims, now = Date.now()) {
  return Array.isArray(claims.amr) && claims.amr.some((method) => {
    if (!method || typeof method !== "object" || method.method !== "recovery") return false;
    const timestamp = Number(method.timestamp) * 1_000;
    return Number.isFinite(timestamp) && timestamp <= now + 30_000 && now - timestamp <= 15 * 60_000;
  });
}
