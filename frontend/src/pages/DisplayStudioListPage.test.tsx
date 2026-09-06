import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi, beforeEach } from "vitest";
import DisplayStudioListPage from "@/pages/DisplayStudioListPage";
import { displayTemplateApi } from "@/lib/endpoints";
import { TEMPLATE_PRESETS } from "@/lib/templatePresets";
import type { DisplayTemplate, MyOrganization } from "@/lib/types";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("@/lib/endpoints", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/endpoints")>();
  return {
    ...actual,
    displayTemplateApi: {
      ...actual.displayTemplateApi,
      listForOrg: vi.fn(),
      create: vi.fn(),
      save: vi.fn(),
    },
  };
});

let activeOrg: MyOrganization | null = null;
vi.mock("@/auth/OrgContext", () => ({
  useOrg: () => ({
    activeOrg,
    organizations: activeOrg ? [activeOrg] : [],
    isLoading: false,
    isFetching: false,
    setActiveOrgId: vi.fn(),
    refetch: vi.fn(),
  }),
}));

function makeOrg(overrides: Partial<MyOrganization> = {}): MyOrganization {
  return {
    id: "org1",
    name: "Org One",
    legal_name: null,
    country: "CA",
    timezone: "UTC",
    currency: "CAD",
    nonprofit_type: null,
    join_code: "AB2CD3EF",
    status: "active",
    plan_id: "plan1",
    subscription_status: "trialing",
    grace_period_ends_at: null,
    role: "owner",
    membership_status: "active",
    ...overrides,
  };
}

function makeTemplate(overrides: Partial<DisplayTemplate> = {}): DisplayTemplate {
  return {
    id: "tpl1",
    organization_id: "org1",
    name: "Untitled template",
    canvas: { width: 1920, height: 1080, background_color: "#0b1220" },
    is_default: false,
    version: 1,
    elements: [],
    ...overrides,
  };
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={queryClient}>
      <DisplayStudioListPage />
    </QueryClientProvider>
  );
  return { ...view, queryClient };
}

// React Query's mutation/query settling can land a microtask after the
// specific assertion a test's own waitFor was watching for, which trips
// React's "update not wrapped in act" warning even though the test itself
// already passed. Waiting for the query client to have nothing in flight
// (not just the one field a test cares about) avoids that noise.
async function settleQueries(queryClient: QueryClient) {
  await act(async () => {
    await waitFor(() => {
      expect(queryClient.isFetching()).toBe(0);
      expect(queryClient.isMutating()).toBe(0);
    });
  });
}

beforeEach(() => {
  navigateMock.mockReset();
  vi.mocked(displayTemplateApi.listForOrg).mockReset();
  vi.mocked(displayTemplateApi.create).mockReset();
  vi.mocked(displayTemplateApi.save).mockReset();
});

describe("DisplayStudioListPage preset picker", () => {
  it("hides the New template button entirely for a role that can't edit", async () => {
    activeOrg = makeOrg({ role: "auditor" });
    vi.mocked(displayTemplateApi.listForOrg).mockResolvedValue([]);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText(/No templates yet/)).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: "New template" })).not.toBeInTheDocument();
  });

  it("shows a blank option and all four presets when New template is clicked", async () => {
    activeOrg = makeOrg({ role: "owner" });
    vi.mocked(displayTemplateApi.listForOrg).mockResolvedValue([]);
    const user = userEvent.setup();

    renderPage();
    await waitFor(() => expect(screen.getByText(/No templates yet/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "New template" }));

    expect(screen.getByRole("button", { name: "Blank template" })).toBeInTheDocument();
    expect(TEMPLATE_PRESETS).toHaveLength(4);
    for (const preset of TEMPLATE_PRESETS) {
      expect(screen.getByText(preset.label)).toBeInTheDocument();
      expect(screen.getByText(preset.description)).toBeInTheDocument();
    }
  });

  it("creates a blank template via the plain create endpoint and navigates to it", async () => {
    activeOrg = makeOrg({ role: "owner" });
    vi.mocked(displayTemplateApi.listForOrg).mockResolvedValue([]);
    vi.mocked(displayTemplateApi.create).mockResolvedValue(makeTemplate({ id: "blank1" }));
    const user = userEvent.setup();

    const { queryClient } = renderPage();
    await waitFor(() => expect(screen.getByText(/No templates yet/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "New template" }));
    await user.click(screen.getByRole("button", { name: "Blank template" }));

    await settleQueries(queryClient);
    expect(navigateMock).toHaveBeenCalledWith("/display-studio/blank1");
    expect(displayTemplateApi.save).not.toHaveBeenCalled();
  });

  it("creates then immediately saves the chosen preset's canvas and elements, and navigates to it", async () => {
    activeOrg = makeOrg({ role: "owner" });
    vi.mocked(displayTemplateApi.listForOrg).mockResolvedValue([]);
    const created = makeTemplate({ id: "created1", version: 1, is_default: false });
    vi.mocked(displayTemplateApi.create).mockResolvedValue(created);
    const preset = TEMPLATE_PRESETS.find((p) => p.key === "goal-thermometer")!;
    const saved = makeTemplate({
      id: "created1",
      name: preset.label,
      canvas: preset.canvas,
      version: 2,
      elements: preset.elements.map((e, i) => ({ ...e, id: `el${i}` })),
    });
    vi.mocked(displayTemplateApi.save).mockResolvedValue(saved);
    const user = userEvent.setup();

    const { queryClient } = renderPage();
    await waitFor(() => expect(screen.getByText(/No templates yet/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "New template" }));
    await user.click(screen.getByText(preset.label));

    await settleQueries(queryClient);
    expect(displayTemplateApi.save).toHaveBeenCalledWith("org1", "created1", {
      name: preset.label,
      canvas: preset.canvas,
      is_default: created.is_default,
      expected_version: created.version,
      elements: preset.elements,
    });
    expect(navigateMock).toHaveBeenCalledWith("/display-studio/created1");
  });

  it("surfaces the backend's error message and leaves the user on the list page if creation fails", async () => {
    activeOrg = makeOrg({ role: "owner" });
    vi.mocked(displayTemplateApi.listForOrg).mockResolvedValue([]);
    vi.mocked(displayTemplateApi.create).mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();

    const { queryClient } = renderPage();
    await waitFor(() => expect(screen.getByText(/No templates yet/)).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "New template" }));
    await user.click(screen.getByRole("button", { name: "Blank template" }));

    await settleQueries(queryClient);
    expect(screen.getByText("Could not create a template.")).toBeInTheDocument();
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
