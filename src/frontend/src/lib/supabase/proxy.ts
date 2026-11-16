import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";
import { isRecoverySession } from "@/lib/auth-session";

export async function updateSession(request: NextRequest) {
  let response = NextResponse.next({ request });
  const supabase = createServerClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY!,
    {
      cookies: {
        getAll: () => request.cookies.getAll(),
        setAll(cookiesToSet) {
          cookiesToSet.forEach(({ name, value }) => request.cookies.set(name, value));
          response = NextResponse.next({ request });
          cookiesToSet.forEach(({ name, value, options }) => response.cookies.set(name, value, options));
        },
      },
    },
  );

  const { data } = await supabase.auth.getClaims();
  const protectedPath = ["/chat", "/dashboard", "/sdk-guide", "/applications", "/available-models", "/model-pools", "/models", "/providers", "/requests", "/reports", "/routing-policies", "/policy-reviews", "/usage", "/billing", "/incidents", "/update-password"].some((path) => request.nextUrl.pathname === path || request.nextUrl.pathname.startsWith(`${path}/`));
  const pendingRecovery = data?.claims && isRecoverySession(data.claims) && request.nextUrl.pathname !== "/update-password";
  let userValid = Boolean(data?.claims);
  let pendingEmail = false;
  if (protectedPath && data?.claims) {
    // Read authoritative Auth state; editable user metadata is not proof that
    // an email has been confirmed, and a stale session must not bypass it.
    const result = await supabase.auth.getUser();
    const user = result.data.user;
    userValid = !result.error && Boolean(user) && user?.is_anonymous !== true;
    pendingEmail = userValid && !user?.email_confirmed_at;
  }
  if (protectedPath && (!userValid || pendingEmail || pendingRecovery)) {
    const url = request.nextUrl.clone();
    url.pathname = !userValid ? "/login" : pendingEmail ? "/sign-up" : "/update-password";
    url.search = pendingEmail ? "?verify=1" : "";
    const redirect = NextResponse.redirect(url);
    response.cookies.getAll().forEach((cookie) => redirect.cookies.set(cookie));
    return redirect;
  }
  return response;
}
