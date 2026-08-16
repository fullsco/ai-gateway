import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Login from "../app/login/page";

const signInWithPassword = vi.fn();
vi.mock("../lib/supabase/browser", () => ({ createClient: () => ({ auth: { signInWithPassword } }) }));

describe("login", () => {
  it("renders required operator fields", () => { render(<Login />); expect(screen.getByLabelText("Email")).toBeRequired(); expect(screen.getByLabelText("Password")).toBeRequired(); });
  it("shows authentication errors clearly", async () => { signInWithPassword.mockResolvedValueOnce({ error: { message: "Invalid login" } }); render(<Login />); fireEvent.change(screen.getByLabelText("Email"), { target: { value: "operator@example.com" } }); fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong" } }); fireEvent.submit(screen.getByRole("button", { name: "Enter control plane" })); expect(await screen.findByText("Invalid login")).toBeVisible(); });
});
