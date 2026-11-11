from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import json
import base64
from dotenv import load_dotenv
import os
import redis

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GITHUB = os.getenv("GITHUB_API_KEY")
REDIS = os.getenv("REDIS_URL")

MODEL = "openai/gpt-oss-20b"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"

session = redis.Redis.from_url(REDIS)


# -------------------- AI RESPONSE --------------------

def generate_ai(prompt):
    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        }

        response = requests.post(
            GROQ_API,
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"]

        print(f"Groq API error: {response.status_code} - {response.text}")

        return (
            f"Error generating AI response. "
            f"Status code: {response.status_code}. "
            f"Details: {response.text}"
        )

    except Exception as e:
        print(f"Error with Groq API: {e}")
        return f"Error generating AI response. Exception: {e}"


# -------------------- GITHUB USER DATA --------------------

def getUserData(username):
    try:
        headers = {
            "Authorization": f"token {GITHUB}"
        }

        response = requests.get(
            f"https://api.github.com/users/{username}/repos",
            headers=headers,
            params={"per_page": 100},
            timeout=30
        )

        if response.status_code == 200:
            repositories = response.json()

            repos = {
                repo["name"]: {
                    "description": repo["description"],
                    "language": repo["language"],
                    "topics": repo["topics"],
                    "created_at": repo["created_at"],
                    "updated_at": repo["updated_at"],
                    "readme": None
                }
                for repo in repositories
            }

            for repo in repos:
                try:
                    readme = requests.get(
                        f"https://api.github.com/repos/"
                        f"{username}/{repo}/contents/README.md",
                        headers=headers,
                        timeout=30
                    )

                    if readme.status_code == 200:
                        repos[repo]["readme"] = base64.b64decode(
                            readme.json()["content"]
                        ).decode("utf-8")

                except Exception as e:
                    print(f"Error fetching README for {repo}: {e}")

            return repos

        print(
            f"GitHub API error: {response.status_code} - "
            f"{response.text}"
        )
        return None

    except Exception as e:
        print(f"Error fetching GitHub data: {e}")
        return None


# -------------------- DEVELOPER SUMMARY --------------------

def aiResponseUser(username):
    repos = getUserData(username)

    if not repos:
        return "Error fetching repository data. Check the GitHub username and try again."

    prompt = (
        "The following JSON contains projects developed by a GitHub user.\n"
        "Provide a plain-text summary of the developer's skills, "
        "experience, and development profile based on these projects. "
        "Explain the projects, technologies, and key features. "
        "Use gender-neutral language unless the user's gender is known.\n\n"
        f"Repository data:\n{json.dumps(repos)}"
    )

    return generate_ai(prompt)


# -------------------- REPOSITORY DATA --------------------

def getRepoData(username, reponame):
    try:
        headers = {
            "Authorization": f"token {GITHUB}"
        }

        response = requests.get(
            f"https://api.github.com/repos/"
            f"{username}/{reponame}/contents/README.md",
            headers=headers,
            timeout=30
        )

        if response.status_code == 200:
            return base64.b64decode(
                response.json()["content"]
            ).decode("utf-8")

        print(
            f"GitHub README error: {response.status_code} - "
            f"{response.text}"
        )
        return None

    except Exception as e:
        print(f"Error fetching README for {reponame}: {e}")
        return None


# -------------------- REPOSITORY SUMMARY --------------------

def aiResponseRepo(username, reponame):
    readme = getRepoData(username, reponame)

    if not readme:
        return "Error fetching repository README. Check the username and repository name."

    prompt = (
        "Explain the following project's README in detail. "
        "Describe its purpose, features, technologies, and how it works. "
        "Use clear paragraphs.\n\n"
        f"README:\n{readme}"
    )

    return generate_ai(prompt)


# -------------------- FLASK APP --------------------

app = Flask(__name__)
CORS(app)


@app.route("/devinfo/<username>")
def devsummary(username):
    return jsonify({
        "summary": aiResponseUser(username)
    })


@app.route("/repoinfo/<username>/<reponame>")
def reposummary(username, reponame):
    return jsonify({
        "summary": aiResponseRepo(username, reponame)
    })


# -------------------- LOAD DEVELOPER DATA --------------------

@app.route("/load/<username>")
def loadprofile(username):
    repos = getUserData(username)

    if repos:
        try:
            session.set(
                username,
                json.dumps(repos),
                ex=600
            )

            return jsonify({
                "message": "Data loaded successfully"
            }), 200

        except Exception as e:
            print(f"Redis caching error: {e}")

            return jsonify({
                "error": "Internal Server Error",
                "message": "Failed to cache data."
            }), 500

    return jsonify({
        "error": "Not Found",
        "message": "GitHub user not found."
    }), 404


# -------------------- LOAD REPOSITORY DATA --------------------

@app.route("/load/<username>/<reponame>")
def loadrepo(username, reponame):
    readme = getRepoData(username, reponame)

    if readme:
        try:
            session.set(
                f"{username}/{reponame}",
                json.dumps(readme),
                ex=600
            )

            return jsonify({
                "message": "Data loaded successfully"
            }), 200

        except Exception as e:
            print(f"Redis caching error: {e}")

            return jsonify({
                "error": "Internal Server Error",
                "message": "Failed to cache data."
            }), 500

    return jsonify({
        "error": "Not Found",
        "message": "Repository README not found."
    }), 404


# -------------------- DEVELOPER Q&A --------------------

@app.route("/chatdev", methods=["POST"])
def chatUser():
    data = request.get_json()

    if not data or "username" not in data or "prompt" not in data:
        return jsonify({
            "error": "Bad Request",
            "message": "Missing username or prompt."
        }), 400

    username = data["username"]
    user_prompt = data["prompt"]

    dev = session.get(username)

    if not dev:
        return jsonify({
            "error": "Not Found",
            "message": "No cached data found. Set the GitHub username first."
        }), 404

    try:
        history_key = f"chat_history:{username}"
        stored_history = session.get(history_key)

        if stored_history:
            history = json.loads(stored_history.decode())
        else:
            history = []

        prompt = (
            "Answer the user's question using the GitHub developer data "
            "provided below. Use the conversation history when relevant. "
            "Do not mention the conversation history itself.\n\n"
            f"Developer data:\n{dev.decode()}\n\n"
            f"Conversation history:\n{json.dumps(history)}\n\n"
            f"Question:\n{user_prompt}"
        )

        answer = generate_ai(prompt)

        if answer.startswith("Error generating AI response."):
            return jsonify({
                "error": "AI API Error",
                "message": answer
            }), 502

        history.append({"user": user_prompt})
        history.append({"assistant": answer})

        session.set(
            history_key,
            json.dumps(history),
            ex=600
        )

        return answer, 200

    except Exception as e:
        print(f"Developer chat error: {e}")

        return jsonify({
            "error": "Internal Server Error",
            "message": str(e)
        }), 500


# -------------------- REPOSITORY Q&A --------------------

@app.route("/chatrepo", methods=["POST"])
def chatRepo():
    data = request.get_json()

    if (
        not data
        or "username" not in data
        or "repository" not in data
        or "prompt" not in data
    ):
        return jsonify({
            "error": "Bad Request",
            "message": "Missing username, repository, or prompt."
        }), 400

    username = data["username"]
    repository = data["repository"]
    user_prompt = data["prompt"]

    repo_key = f"{username}/{repository}"
    repo = session.get(repo_key)

    if not repo:
        return jsonify({
            "error": "Not Found",
            "message": "No cached repository data found. Set the repository first."
        }), 404

    try:
        history_key = f"chat_history:{repo_key}"
        stored_history = session.get(history_key)

        if stored_history:
            history = json.loads(stored_history.decode())
        else:
            history = []

        prompt = (
            "Answer the user's question using the project README provided "
            "below. Use the conversation history when relevant. "
            "Do not mention the conversation history itself.\n\n"
            f"Project README:\n{repo.decode()}\n\n"
            f"Conversation history:\n{json.dumps(history)}\n\n"
            f"Question:\n{user_prompt}"
        )

        answer = generate_ai(prompt)

        if answer.startswith("Error generating AI response."):
            return jsonify({
                "error": "AI API Error",
                "message": answer
            }), 502

        history.append({"user": user_prompt})
        history.append({"assistant": answer})

        session.set(
            history_key,
            json.dumps(history),
            ex=600
        )

        return answer, 200

    except Exception as e:
        print(f"Repository chat error: {e}")

        return jsonify({
            "error": "Internal Server Error",
            "message": str(e)
        }), 500


# -------------------- RUN SERVER --------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)