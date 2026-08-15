import { createClient } from "@/lib/supabase/server";
import { NextRequest, NextResponse } from "next/server";

async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const supabase = await createClient();
  const { data } = await supabase.auth.getUser();
  if (!data.user) return NextResponse.json({ error: "not_authenticated" }, { status: 401 });
  const { data: sessionData } = await supabase.auth.getSession();
  if (!sessionData.session) return NextResponse.json({ error: "not_authenticated" }, { status: 401 });
  if (!["GET", "HEAD", "OPTIONS"].includes(request.method)) {
    const origin = request.headers.get("origin");
    if (origin && origin !== request.nextUrl.origin) {
      return NextResponse.json({ error: "invalid_origin" }, { status: 403 });
    }
  }
  const { path } = await context.params;
  const base = process.env.GATEWAY_ADMIN_API_URL;
  if (!base) return NextResponse.json({ error: "gateway_api_not_configured" }, { status: 503 });
  const target = new URL(`/api/admin/v1/${path.join("/")}`, base);
  request.nextUrl.searchParams.forEach((value, key) => target.searchParams.set(key, value));
  const body = ["GET", "HEAD"].includes(request.method) ? undefined : await request.text();
  const response = await fetch(target, {
    method: request.method,
    headers: {
      authorization: `Bearer ${sessionData.session.access_token}`,
      "content-type": request.headers.get("content-type") ?? "application/json",
    },
    body,
    cache: "no-store",
  });
  return new NextResponse(response.body, {
    status: response.status,
    headers: {
      "content-type": response.headers.get("content-type") ?? "application/json",
      ...(response.headers.get("x-request-id")
        ? { "x-request-id": response.headers.get("x-request-id") as string }
        : {}),
    },
  });
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
