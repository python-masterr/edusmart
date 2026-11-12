const message = document.getElementById("message");

async function adminRequest(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      ...options,
      credentials: "same-origin",
      headers: {
        ...(options.headers || {}),
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
    });
  } catch {
    throw new Error("Could not reach the EduSmart server.");
  }

  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.detail || "The request failed.");
    error.status = response.status;
    throw error;
  }
  return data;
}

const loginForm = document.getElementById("adminLoginForm");
if (loginForm) {
  const params = new URLSearchParams(window.location.search);
  if (params.get("error") === "use_admin_sign_in") {
    message.textContent = "Use the administrator sign-in form for this account.";
  }

  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = document.getElementById("submitButton");
    const formData = new FormData(loginForm);
    button.disabled = true;
    message.textContent = "";
    try {
      await adminRequest("/api/admin/login", {
        method: "POST",
        body: JSON.stringify({
          email: formData.get("email"),
          password: formData.get("password"),
        }),
      });
      loginForm.reset();
      window.location.assign("/admin");
    } catch (error) {
      message.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
} else {
  adminRequest("/api/admin/overview")
    .then((data) => {
      document.getElementById("adminIdentity").textContent =
        `Signed in as ${data.admin.name} (${data.admin.email})`;
      document.getElementById("userCount").textContent = data.users;
      document.getElementById("documentCount").textContent = data.documents;
      document.getElementById("quizCount").textContent = data.quizzes;
      document.getElementById("overview").hidden = false;
    })
    .catch((error) => {
      if (error.status === 401) {
        window.location.replace("/admin/login");
        return;
      }
      message.textContent = error.message;
    });

  document.getElementById("logoutButton").addEventListener("click", async () => {
      const button = document.getElementById("logoutButton");
      button.disabled = true;
      try {
        await adminRequest("/api/admin/logout", { method: "POST" });
        window.location.assign("/admin/login");
      } catch (error) {
        message.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
}
