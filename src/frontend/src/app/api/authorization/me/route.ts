import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const authorization = request.headers.get("authorization");
  if (!authorization) {
    return NextResponse.json({ detail: "Authentication required" }, { status: 401 });
  }

  const backendUrl = process.env.BACKEND_API_URL
    ?? process.env.NEXT_PUBLIC_API_URL
    ?? "http://127.0.0.1:8000";

  try {
    const response = await fetch(`${backendUrl.replace(/\/$/, "")}/api/v1/authorization/me`, {
      headers: { Authorization: authorization },
      cache: "no-store",
    });
    const contentType = response.headers.get("content-type") ?? "";
    const body = contentType.includes("application/json")
      ? await response.json()
      : { detail: await response.text() || "Authorization service returned an invalid response." };
    return NextResponse.json(body, { status: response.status, headers: { "Cache-Control": "private, no-store" } });
  } catch (error) {
    console.error("Authorization proxy failed", error);
    return NextResponse.json({ detail: "Authorization service is unavailable." }, { status: 503 });
  }
}
